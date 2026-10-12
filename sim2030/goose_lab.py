"""离线 GOOSE 接收实验。规则和容量是显式实验参数，不代表具体厂家终端。"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import replace
from pathlib import Path

from sim2030.constants import BusinessType
from sim2030.contracts import Message
from sim2030.goose_capture import SAMPLES, GooseCapture, epoch_us, iso_time, observation_events, reference_checks


PROFILES = {"normal": 5000, "limited": 100}
# 手动下发帧使用负编号，且与接收队列控制命令（-1、-2…）分属不同编号段，避免与循环回放的基线帧编号冲突。
MANUAL_ID_BASE = 1_000_000
FAULT_STARTS = {"normal": 900000, "forgery": None, "suppression": 1100000, "dos": 900000}


def configure_lab(config, options=None):
    """只允许选择已知样本和预设策略，在关闭旧运行前完成参数检查。"""
    if not config.layout.get("goose_lab"):
        if options:
            raise ValueError("该场景不支持 GOOSE 接收实验参数")
        return
    if options is not None and not isinstance(options, dict):
        raise ValueError("goose_options 必须是对象")
    options = options or {}
    if set(options) - {"sample", "policy", "capacity"}:
        raise ValueError("未知 GOOSE 实验参数")
    settings = dict(config.layout["goose_lab"])
    settings.update(options)
    for key, allowed in (("sample", SAMPLES), ("policy", ("basic", "validated")), ("capacity", PROFILES)):
        if not isinstance(settings.get(key), str) or settings[key] not in allowed:
            raise ValueError(f"无效的 GOOSE 参数：{key}")
    sample = settings["sample"]
    settings.update(service_fps=PROFILES[settings["capacity"]], queue_capacity=64,
                    fault_start_us=FAULT_STARTS[sample], fault_current_a=900,
                    trip_deadline_us=400000)
    settings["manual_dispatch"] = True
    config.simulation["duration_us"] = 600_000_000
    config.layout["goose_lab"] = settings
    config.layout["packet_replay"]["sample"] = sample
    config.observations["provider_dir"] = str(Path(config.observations["provider_dir"]).parent / sample)
    config.environment["fault"] = {"start_time_us": settings["fault_start_us"], "current_a": 900}


class GooseLab:
    """到达/服务按微秒排队；被接受的跳闸在下一仿真步进入智能终端命令接口。"""

    def __init__(self, capture, settings):
        self.capture, self.settings = capture, dict(settings)
        if not capture.records:
            raise ValueError("样本没有可回放的帧，无法建立接收实验")
        self.baseline_count = len(capture.records)
        if self.baseline_count >= 2:
            spacing = capture.times[-1] - capture.times[-2]
            self.cycle_us = max(1, capture.times[-1] + max(0, spacing))
        else:
            self.cycle_us = max(1, capture.times[0]) if capture.times else 1
        self.expected = capture.manifest["goose_config"]
        self.publishers = set(capture.meta["baseline"]["publishers"].get(self.expected["gocbRef"], []))
        if not self.publishers:
            raise ValueError("缺少正常样本来源配置，无法建立接收实验")
        self.service_us = (1000000 + settings["service_fps"] - 1) // settings["service_fps"]
        self.queue = deque()
        self.cursor = 0
        self.counts = Counter()
        self.decisions = {}
        self.events, self.pending_records, self.history = [], [], []
        self.pending_trips = []
        self.last_st = self.last_sq = self.last_trip = None
        self.deadline = None
        self.expired = False
        self.max_queue = self.max_delay_us = 0
        self.first_trip_frame = self.trip_dispatched_us = self.opened_us = None
        self.fault_started = False
        self.was_closed = True
        self.time_us = -1
        self._event_buckets = set()
        self._feedback_ids = set()
        self.control_messages = {}
        self.pending_controls = []
        self.open_cause = None
        self.manual_frames = {}
        self.manual_pending = []
        self.manual_sent = 0
        self.observed_records, self.observed_points = [], []
        self._observation_batch = []
        self._dispatched_messages = []
        self.dos_capture = None
        self.suppression_capture = None
        sample_dir = capture.directory.parent
        dos_dir = sample_dir / "dos"
        suppression_dir = sample_dir / "suppression"
        if capture.meta["sample"] == "dos":
            self.dos_capture = capture
        elif (dos_dir / "capture-index.json").is_file():
            self.dos_capture = GooseCapture(dos_dir)
        if capture.meta["sample"] == "suppression":
            self.suppression_capture = capture
        elif (suppression_dir / "capture-index.json").is_file():
            self.suppression_capture = GooseCapture(suppression_dir)

    def receive_control(self, message, now, engine):
        """将正常网络控制命令纳入同一 FIFO；不是把它伪装成原始 GOOSE 帧。"""
        terminal = engine.get_device("ctl_brk")
        if (message.receiver_id != "ctl_brk" or message.business_type != BusinessType.COMMAND
                or message.target_port != terminal.up_port):
            return False
        number = -len(self.control_messages) - 1
        self.control_messages[number] = message
        payload = message.payload or {}
        request_id = payload.get("request_id") or message.related_request or message.message_id
        action = payload.get("action_type", "")
        label = {"open": "分闸", "close": "合闸", "trip": "跳闸"}.get(action, action)
        self.counts["received"] += 1
        occupied, capacity = len(self.queue), self.settings["queue_capacity"]
        self.decisions[number] = {"frame_no": number, "kind": "control", "label": f"{label}命令 #{-number}",
                                  "source": message.sender_id, "request_id": request_id, "action": action,
                                  "message_id": message.message_id, "arrival_time_us": now,
                                  "arrival_order": self.counts["received"], "queue_on_arrival": occupied,
                                  "capacity_on_arrival": capacity, "status": "queued", "reason": "等待共享接收队列处理"}
        if occupied >= capacity:
            reason = f"到达时接收队列已满（{occupied}/{capacity}），{label}命令未执行"
            self._finish(number, now, "dropped", reason)
            status = "rejected"
        else:
            end = max(now, self.queue[-1][0] if self.queue else now) + self.service_us
            self.queue.append((end, number))
            self.max_queue = max(self.max_queue, len(self.queue))
            reason = f"{label}命令已入队，等待处理（入队前 {occupied}/{capacity}）"
            self._event(now, "control_queued", reason)
            status = "queued"
        # 通过智能终端原有上行反馈通道返回测控/站控端。
        terminal.receive(Message(message_id=f"queue-feedback-{message.message_id}",
                                 sender_id="receiver-queue", receiver_id="ctl_brk", business_type=BusinessType.FEEDBACK,
                                 related_request=request_id, created_time_us=now,
                                 payload={"request_id": request_id, "action_id": request_id, "status": status,
                                          "reason": reason, "action_type": action}))
        return True

    def frame_record(self, number):
        if number in self.manual_frames:
            return self.manual_frames[number]["record"]
        return self.capture.records[(number - 1) % self.baseline_count]

    def dispatch(self, request_id, now, dt_us, count=1, flood=False, suppression=False):
        if suppression:
            capture = self.suppression_capture
            # 消息抑制模板固定发送样本内全部投毒帧（高 stNum 心跳及其重传）。
            if capture is not None and capture.attack_indices:
                count = len(capture.attack_indices)
        else:
            capture = self.dos_capture if flood else self.capture
        if type(count) is not int or not 1 <= count <= 5000:
            return {"status": "rejected", "reason": "每批数量必须是 1–5000 的整数"}
        if capture is None or not capture.attack_indices:
            return {"status": "rejected", "reason": "未导入对应攻击样本，请先导入 message_core.zip"}
        if len(self.manual_pending) + count > 10000 or self.manual_sent + count > 20000:
            return {"status": "rejected", "reason": "待发送上限 10000 帧、每轮上限 20000 帧；请等待或重新加载场景"}
        for index_in_batch in range(count):
            index = capture.attack_indices[self.manual_sent % len(capture.attack_indices)]
            self.manual_sent += 1
            number = -(MANUAL_ID_BASE + self.manual_sent)
            frame = capture.records[index]
            message = Message(message_id=f"{request_id}:{index_in_batch + 1}", sender_id="sw-process", receiver_id="ctl_brk",
                              business_type="goose_attack", source_port="injection", target_port="up",
                              created_time_us=now, deliver_time_us=now + dt_us,
                              payload={"protocol": "GOOSE", "attack_frame": True, "sample_frame_no": index + 1,
                                       "sample": capture.meta["sample"], "record": frame, "batch_id": request_id,
                                       "batch_count": count, "batch_index": index_in_batch + 1, "receiver_frame_no": number,
                                       "manual_ordinal": self.manual_sent,
                                       "note": "手动下发的样本帧实例；原始采集时间保留在 record 内"}).to_record()
            self.manual_frames[number] = {"record": frame, "message": message}
            self.manual_pending.append((message["deliver_time_us"], number))
            self._dispatched_messages.append(message)
        if suppression:
            label = f"交换机下发 {count} 帧消息抑制报文（状态号投毒）"
        elif flood:
            label = f"交换机批量下发 {count} 帧洪泛流量"
        else:
            label = "交换机手动下发一帧攻击报文"
        self._event(now, "manual_dispatch", label)
        return {"status": "accepted", "count": count, "batch_id": request_id}

    def drain_dispatch_messages(self):
        messages, self._dispatched_messages = self._dispatched_messages, []
        return messages

    def in_flight(self):
        # 动画取真实实例的前 24 帧；队列推进与统计仍处理所有实例。
        return [self.manual_frames[number]["message"] for _, number in self.manual_pending[:24]]

    def configure_receiver(self, parameters, now):
        if not isinstance(parameters, dict) or not parameters or set(parameters) - {"queue_capacity", "entry_filter", "service_fps"}:
            return {"status": "rejected", "reason": "接收配置参数无效"}
        capacity = parameters.get("queue_capacity", self.settings["queue_capacity"])
        rate = parameters.get("service_fps", self.settings["service_fps"])
        enabled = parameters.get("entry_filter", self.settings["policy"] == "validated")
        if type(capacity) is not int or not 1 <= capacity <= 4096:
            return {"status": "rejected", "reason": "队列容量必须是 1–4096 帧的整数"}
        if capacity < len(self.queue):
            return {"status": "rejected", "reason": f"当前队列占用 {len(self.queue)} 帧，请先等待处理或设置更大的容量"}
        if type(rate) is not int or not 10 <= rate <= 50000 or type(enabled) is not bool:
            return {"status": "rejected", "reason": "处理速率须为 10–50000 帧/秒的整数，入口过滤须为开关值"}
        self.settings.update(queue_capacity=capacity, service_fps=rate, policy="validated" if enabled else "basic")
        self.service_us = (1000000 + rate - 1) // rate
        self._event(now, "receiver_config", f"终端配置更新：容量 {capacity} 帧，速率 {rate} 帧/秒，入口过滤{'启用' if enabled else '关闭'}")
        return {"status": "accepted", "settings": dict(self.settings)}

    def baseline_label(self, number):
        """循环回放的基线帧按样本内位置和轮次标注，编号本身会随轮次增长。"""
        position = (number - 1) % self.baseline_count + 1
        lap = (number - 1) // self.baseline_count + 1
        return f"基线 #{position}" + (f"（第{lap}轮）" if lap > 1 else "")

    def receiver_row(self, number):
        decision = self.decisions[number]
        if number in self.control_messages:
            return dict(decision)
        frame = self.frame_record(number)
        manual = self.manual_frames.get(number)
        ordinal = manual["message"]["payload"].get("manual_ordinal") if manual else None
        return {**decision, "label": f"下发 #{ordinal}" if manual else self.baseline_label(number),
                "source": "交换机" if manual else "保护装置", "appid": frame["appid"],
                "stNum": frame["goose"].get("stNum"), "sqNum": frame["goose"].get("sqNum"),
                "trip": frame["dataset"][0][0], "message_id": manual["message"]["message_id"] if manual else None}

    def receiver_view(self, before=None, limit=30):
        limit = max(1, min(50, limit))
        completed = [number for number, decision in self.decisions.items()
                     if decision["status"] not in {"not_sent", "queued"}
                     and (before is None or decision["arrival_order"] < before)]
        completed.sort(key=lambda number: self.decisions[number]["arrival_order"], reverse=True)
        selected = completed[:limit]
        queue = list(self.queue)
        return {"settings": dict(self.settings), "occupancy": len(queue), "peak": self.max_queue,
                "counts": dict(self.counts), "pending": len(self.manual_pending), "max_delay_us": self.max_delay_us,
                "queue_frames": [{**self.receiver_row(number), "complete_time_us": end} for end, number in queue[:50]],
                "queue_truncated": len(queue) > 50, "rows": [self.receiver_row(number) for number in selected],
                "has_older": len(completed) > limit,
                "oldest_order": self.decisions[selected[-1]]["arrival_order"] if selected else None}

    def observations(self, now):
        batch, self._observation_batch = self._observation_batch, []
        if not batch:
            return []
        groups = {}
        for number, arrival in batch:
            sample = self.manual_frames[number]["message"]["payload"]["sample"] if number in self.manual_frames else self.settings["sample"]
            groups.setdefault(sample, []).append((number, arrival))
        events = []
        for sample, rows in groups.items():
            if sample == "dos":
                capture = self.dos_capture
            elif sample == "suppression":
                capture = self.suppression_capture
            else:
                capture = self.capture
            records = [self.frame_record(number) for number, _ in rows]
            points = [{"frame_no": record["frame_no"], "time_us": arrival, "length": record["frame_length"]}
                      for record, (_, arrival) in zip(records, rows)]
            generated = observation_events(records, points, capture.manifest, sample, "substation-goose-lab")
            stamp = iso_time(epoch_us(self.capture.manifest["base_time_utc"]) + now)
            for i, event in enumerate(generated):
                event.update(event_id=f"dispatch-{sample}-{now}-{i}", timestamp=stamp, observed_at=stamp,
                             time_source="manual-simulation-clock", raw_data_uri=str(capture.directory / "capture.pcap"),
                             raw_metadata_uri=str(capture.directory / "manifest.json"))
                event["metadata"]["note"] = "只统计实际到达的帧；原始帧为模板，实验时间独立记录"
            events.extend(generated)
        return events

    def _event(self, time_us, kind, text, frame_no=None, coalesce=False):
        key = (kind, time_us // 1000000)
        if coalesce and key in self._event_buckets:
            return
        self._event_buckets.add(key)
        event = {"time_us": time_us, "kind": kind, "text": text, "frame_no": frame_no if frame_no not in self.manual_frames and frame_no not in self.control_messages else None}
        self.events.append(event)
        self.pending_records.append({"type": "event", **event})

    def _subscription(self, frame):
        goose = frame.get("goose", {})
        return (frame.get("appid") == self.expected["appid"]
                and frame.get("dst_mac", "").lower() == "01:0c:cd:01:30:01"
                and frame.get("vlan", {}).get("vid") == self.expected["vlan_id"]
                and goose.get("gocbRef") == self.expected["gocbRef"])

    def _finish(self, number, now, status, reason):
        decision = self.decisions[number]
        decision.update(status=status, reason=reason, decision_time_us=now,
                        delay_us=now - decision["arrival_time_us"])
        self.max_delay_us = max(self.max_delay_us, decision["delay_us"])
        self.counts[status] += 1
        if number in self.control_messages:
            self.pending_records.append({"type": "decision", "time_us": now, **decision})
            self._event(now, "control_ready" if status == "accepted" else "control_failed", reason)
            return
        frame = self.frame_record(number)
        if status == "accepted" and frame.get("src_mac") not in self.publishers:
            self.counts["unlisted_accepted"] += 1
        if status in {"dropped", "stale", "rejected"} and self._subscription(frame) and frame.get("src_mac") in self.publishers:
            self.counts["expected_not_accepted"] += 1
        self.pending_records.append({"type": "decision", "time_us": now, **decision})
        if status != "accepted":
            self._event(now, status, reason, number, coalesce=True)

    def _expire(self, now):
        if self.deadline is not None and now >= self.deadline and not self.expired:
            self.expired = True
            self._event(self.deadline, "timeout", "有效订阅报文超时，保留序号状态并等待有效更新")

    def _complete_until(self, until):
        while self.queue and self.queue[0][0] <= until:
            now, number = self.queue.popleft()
            self._expire(now)
            if number in self.control_messages:
                self._finish(number, now, "accepted", "共享接收处理完成，设备动作另行反馈")
                self.pending_controls.append(number)
                continue
            frame = self.frame_record(number)
            goose = frame.get("goose", {})
            if not self._subscription(frame):
                self._finish(number, now, "filtered", "APPID、组播地址或订阅控制块不匹配")
                continue
            dataset = frame.get("dataset", [])
            if (frame.get("parse_status") != "ok" or len(dataset) != 4
                    or not isinstance(dataset[0], list) or len(dataset[0]) < 2
                    or not isinstance(dataset[0][0], bool)
                    or not isinstance(dataset[0][1], dict)
                    or dataset[0][1].get("value") != 0
                    or goose.get("confRev") != self.expected["confRev"]
                    or goose.get("datSet") != self.expected["datSet"]
                    or goose.get("simulation") or goose.get("ndsCom")):
                self._finish(number, now, "rejected", "结构、配置版本或跳闸数据质量不符合实验订阅要求")
                continue
            age = now - self.decisions[number]["arrival_time_us"]
            ttl = goose.get("timeAllowedtoLive", 0) * 1000
            if ttl <= 0 or age >= ttl:
                self._finish(number, now, "stale", "队列停留已达到报文生存时间，丢弃过期报文")
                continue
            st, sq, trip = goose.get("stNum"), goose.get("sqNum"), dataset[0][0]
            if not isinstance(st, int) or not isinstance(sq, int) or not 1 <= st <= 0xffffffff or not 0 <= sq <= 0xffffffff:
                self._finish(number, now, "rejected", "状态号或序列号无效")
                continue
            changed = st != self.last_st
            if self.last_st is not None:
                delta = (st - self.last_st) & 0xffffffff
                if (changed and not 0 < delta < 0x80000000) or (not changed and (sq <= self.last_sq or trip != self.last_trip)):
                    self._finish(number, now, "rejected", "状态号回退、重复序列或同状态数据发生变化")
                    continue
            self.last_st, self.last_sq, self.last_trip = st, sq, trip
            self.deadline = self.decisions[number]["arrival_time_us"] + ttl
            self.expired = False
            self._finish(number, now, "accepted", "订阅及状态检查通过" + ("；基础策略未校验来源" if self.settings["policy"] == "basic" else "；来源在允许列表内"))
            if changed:
                self._event(now, "accepted", f"接受新状态 stNum={st}，trip={'true' if trip else 'false'}", number)
            if changed and trip and self.first_trip_frame is None:
                self.first_trip_frame = number
                self.pending_trips.append((number, now))
        self._expire(until)

    def advance(self, now, engine):
        fault_start = self.settings["fault_start_us"]
        if fault_start is not None and now >= fault_start and not self.fault_started:
            self.fault_started = True
            self._event(fault_start, "fault", "实验故障投入：合闸时线路电流设为 900 A")
        arrivals = []
        # 基线样本循环回放：走完一整个样本后回到第 1 帧，按样本周期顺延到下一轮。
        while True:
            index = self.cursor % self.baseline_count
            arrival = (self.cursor // self.baseline_count) * self.cycle_us + self.capture.times[index]
            if arrival > now:
                break
            number = self.cursor + 1
            self.cursor += 1
            if self.frame_record(number).get("src_mac") == "00:0c:29:66:00:66":
                self.decisions[number] = {"frame_no": number, "status": "not_sent", "reason": "样本攻击帧未自动投入，请点击交换机手动下发"}
                self.pending_records.append({"type": "decision", "time_us": now, **self.decisions[number]})
            else:
                arrivals.append((arrival, number))
        arrivals.extend((arrival, number) for arrival, number in self.manual_pending if arrival <= now)
        self.manual_pending = [(arrival, number) for arrival, number in self.manual_pending if arrival > now]
        for arrival, number in sorted(arrivals):
            self._complete_until(arrival)
            frame = self.frame_record(number)
            self.decisions[number] = {"frame_no": number, "arrival_time_us": arrival,
                                      "status": "queued", "reason": "等待接收处理"}
            self.counts["received"] += 1
            self.decisions[number]["arrival_order"] = self.counts["received"]
            self.decisions[number].update(queue_on_arrival=len(self.queue), capacity_on_arrival=self.settings["queue_capacity"])
            self.observed_records.append(frame)
            self.observed_points.append({"time_us": arrival})
            self._observation_batch.append((number, arrival))
            if self.settings["policy"] == "validated":
                if not self._subscription(frame):
                    self._finish(number, arrival, "filtered", "入口过滤：不属于本终端订阅")
                    continue
                if frame.get("src_mac") not in self.publishers:
                    self._finish(number, arrival, "rejected", "入口过滤：来源 MAC 不在允许列表内")
                    continue
            if len(self.queue) >= self.settings["queue_capacity"]:
                self._finish(number, arrival, "dropped", "接收队列已满，丢弃新到达帧")
                continue
            end = max(arrival, self.queue[-1][0] if self.queue else arrival) + self.service_us
            self.queue.append((end, number))
            self.max_queue = max(self.max_queue, len(self.queue))
        self._complete_until(now)
        for number in self.pending_controls:
            original = self.control_messages[number]
            # 原指令时间只作为记录；动作计时从排队处理完成后的转交时刻开始。
            payload = {**(original.payload or {}), "original_created_time_us": original.created_time_us,
                       "receiver_arrival_time_us": self.decisions[number]["arrival_time_us"],
                       "receiver_processed_time_us": self.decisions[number]["decision_time_us"]}
            engine.get_device("ctl_brk").receive(replace(original, created_time_us=now, payload=payload))
            self._event(now, "control_forwarded", f"{self.decisions[number]['label']}离开接收队列，转交执行")
        self.pending_controls.clear()
        for number, accepted_us in self.pending_trips:
            request_id = f"goose-frame-{number}"
            engine.get_device("ctl_brk").receive(Message(
                message_id=request_id, sender_id="goose-receiver", receiver_id="ctl_brk",
                business_type=BusinessType.COMMAND, source_port="replay", target_port="up",
                related_request=request_id, created_time_us=now,
                payload={"request_id": request_id, "action_type": "trip", "parameters": {},
                         "goose_frame_no": number, "accepted_time_us": accepted_us}))
            self.trip_dispatched_us = now
            self._event(now, "command", "智能终端下发跳闸，等待断路器执行反馈", number)
        self.pending_trips.clear()
        self.time_us = now

    def observe(self, now, engine, result):
        closed = engine.get_device("brk").physical_outputs()["closed"]
        if self.was_closed and not closed:
            self.opened_us = now
            completed_ids = {message.payload.get("action_id") for message in result.messages
                             if message.sender_id == "brk" and message.business_type == BusinessType.FEEDBACK
                             and message.payload.get("status") == "completed" and message.created_time_us == now}
            control = next((decision for decision in self.decisions.values()
                            if decision.get("kind") == "control" and decision.get("request_id") in completed_ids), None)
            self.open_cause = "control" if control else "goose"
            self._event(now, "breaker", "断路器执行遥控分闸，线路电流与供电功率归零" if control else "断路器实际分闸，线路电流与供电功率归零",
                        None if control else self.first_trip_frame)
        self.was_closed = closed
        env = engine._environment.snapshot()
        self.history.append({"time_us": now, "current_a": env["line_current_a"],
                             "power_mw": env["active_power_mw"], "queue": len(self.queue)})
        self.history = self.history[-80:]
        for message in result.messages:
            if (message.sender_id == "brk" and message.business_type == BusinessType.FEEDBACK
                    and message.payload.get("status") == "completed" and message.message_id not in self._feedback_ids):
                self._feedback_ids.add(message.message_id)
                is_control = any(decision.get("kind") == "control" and decision.get("request_id") == message.payload.get("action_id")
                                 for decision in self.decisions.values())
                self._event(now, "feedback", "断路器发出动作完成反馈", None if is_control else self.first_trip_frame)

    def view(self, engine):
        env = engine._environment.snapshot()
        closed = engine.get_device("brk").physical_outputs()["closed"]
        start = self.settings["fault_start_us"]
        elapsed = max(0, (self.opened_us if self.opened_us is not None else self.time_us) - start) if start is not None else 0
        if not closed:
            if start is not None and self.opened_us is not None and self.opened_us >= start:
                delayed = elapsed > self.settings["trip_deadline_us"]
                outcome, severity = ("故障延迟切除", "warning") if delayed else ("故障切除", "normal")
            elif self.open_cause == "control":
                outcome, severity = "遥控分闸已完成，线路停止供电", "warning"
            else:
                outcome, severity = "无故障跳闸，线路失去供电", "danger"
        elif self.fault_started:
            outcome = "故障持续，跳闸超过实验时限" if elapsed > self.settings["trip_deadline_us"] else "故障已投入，等待跳闸"
            severity = "danger" if elapsed > self.settings["trip_deadline_us"] else "warning"
        else:
            outcome, severity = "线路正常供电", "normal"
        checks, rates = reference_checks(self.observed_records, self.observed_points, self.capture.meta["baseline"])
        return {"reference_check_counts": dict(Counter(issue for check in checks for issue in check)),
                "peak_frames_1s": max(rates, default=0),
                "settings": self.settings, "receiver": self.receiver_view(), "flood_available": self.dos_capture is not None,
                "manual_sent": self.manual_sent, "counts": dict(self.counts), "queue": len(self.queue),
                "max_queue": self.max_queue, "max_delay_us": self.max_delay_us,
                "subscription_expired": self.expired, "receiver_stNum": self.last_st,
                "suppression_available": self.suppression_capture is not None,
                "outcome": outcome, "severity": severity, "breaker_closed": closed,
                "fault_active": bool(env.get("fault_active")), "fault_duration_us": elapsed if self.fault_started else 0,
                "opened_us": self.opened_us, "first_trip_frame": self.first_trip_frame,
                "trip_latency_us": self.opened_us - self.decisions[self.first_trip_frame]["arrival_time_us"] if self.opened_us is not None and self.first_trip_frame and self.open_cause != "control" else None,
                "trip_dispatched_us": self.trip_dispatched_us,
                "current_a": env["line_current_a"], "power_mw": env["active_power_mw"],
                "history": self.history[-80:], "events": sorted(self.events, key=lambda e: e["time_us"])[-24:]}

    def drain_records(self):
        records, self.pending_records = self.pending_records, []
        return records
