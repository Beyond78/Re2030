# message_core 离线报文接入

## 已接入内容

本次交付包为合成 GOOSE 过程层数据，采集点为 `substation-attack-demo/CB-TERMINAL-IN`。发布方、订阅方按 manifest 映射为本项目的过流保护装置 `prot` 和断路器智能终端 `ctl_brk`，采集接口关联到 `span-process`。目的 MAC 是组播地址；该映射表示预期订阅关系，不代表终端已收到或接受报文。

| 样本 | 帧数 | 采集跨度 | 来源 MAC 偏离正常样本 | 状态号回退 | 1 秒峰值 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 正常 | 20 | 2.8s | 0 | 0 | 10 |
| 伪造 | 23 | 3.9s | 3 | 1 | 8 |
| 抑制 | 26 | 3.8s | 2 | 1 | 11 |
| DoS | 3024 | 3.5s | 3000 | 13 | 2010 |

合计 3093 帧。数据文件通过交付包 SHA-256 清单核对，PCAP 使用交付参考脚本的纯解码子集逐帧读回，与通信日志完全一致。`readback` 也与日志逐字节一致；正常回放只消费一份日志，不重复计数。

压缩包实际包含 25 个文件。清单中的 23 个内容文件通过核对；清单另列的《采集口径、payload封装与数据集流转.md》没有包含在压缩包中。现有四组数据可完成此次接入。详细结果保存在本地 `data/message_core/import-report.json`。

## 使用

```powershell
python tools/import_message_core.py "路径/message_core.zip"
python main.py --ui --port 8770
```

已有数据不会被覆盖。需要重新导入时可用 `--output` 指定新的空目录，并同步修改场景中的 `observations.provider_dir`。

页面提供四个“GOOSE 回放”场景。未导入数据时，场景显示“请先导入 message_core.zip”。开始或单步推进后，右侧出现已经到达采集时刻的帧；可以翻页、按帧号查看，或查看最新帧。详情包括微秒时间、MAC、VLAN、APPID、stNum、sqNum、trip、数据集、完整日志及原始帧字节。

## 数据进入系统的方式

通信日志和 PCAP 保留原字节。本地导入器额外生成帧索引和 `events.jsonl`，每 100ms 形成一个 `source_type=ids / event_type=stats` 批次，携带真实帧数、字节数、控制块和源 MAC 集合。批次在区间结束后才交付，供现有 `FileObservationSource → ObservationGateway → system` 通路消费。采集 UTC 时间按包内基准映射到仿真相对时间，微秒精度保留。

GOOSE 逐帧接口是 `GET /api/runs/{run_id}/views?view=goose`。可选参数为 `frame_no`、`before_frame`、`limit`，每页最多 50 帧。接口只读场景已配置的样本目录，不接受任意文件路径，也不返回尚未到达的帧。已结束运行可通过保存的 manifest 和采集文件恢复查看。

样本没有作为仿真 `Message` 或控制动作注入设备。原有量测、分合闸和电磁攻击逻辑保持独立；即使样本里 `trip=true`，也不会因此改变仿真断路器。包内没有终端接收判定或断路器动作记录，这两项结果保持 unknown。

## 参考检查与真值

页面显示三个参考量：同一控制块的源 MAC 是否偏离正常样本、stNum 是否回退、滑动 1 秒帧数峰值。流量参考阈值为正常样本峰值的 5 倍，即本包的 50 帧/秒。窗口左边界不包含恰好 1 秒前的帧。

检查函数只读取正常基线和通信日志，不读取样本名称、攻击类型或标签。状态号回退可能落在受影响的合法帧上，未知来源也可能是基线变更；这些提示不等于攻击成功或成熟 IDS 的判定。未做 R1–R6 全量算法复现，未做现场阈值标定。此版不把提示包装成 IDS 告警，正式识别与防御在四个回放场景中关闭。

`labels-*.jsonl` 仅封存在 `data/message_core/evaluation/`，不进入提供方目录和观测输入。DoS 标签的帧号是生成顺序，与日志采集顺序不同，后续评价不能直接按帧号对齐，需要核对采集时间及必要字段。本次没有计算检测准确率，也没有推断断路器动作。

## 文件与验证

- `tools/import_message_core.py`：校验交付包、核对 PCAP/日志、生成本地索引与观测批次。
- `sim2030/goose_codec.py`：从交付脚本提取的纯解码函数，未包含报文生成器、网络发送或原脚本 CLI。
- `sim2030/goose_capture.py`：PCAP 读取、基线参考检查、分页与时间边界控制。
- `scenarios/substation-goose-*.json`：四个回放场景。
- `application.py`、`presentation/server.py`、`presentation/views.py`：运行与回放接口。
- `presentation/static/`：逐帧查看、参考量和报文详情。

```powershell
python -m unittest tests.test_goose_capture -v
python -m unittest discover -s tests -t . -q
node --check sim2030/presentation/static/app.js
```

测试覆盖完整性校验、损坏 PCAP、标签隔离、批次帧数、时间精度、未来帧隔离、分页、加载失败保留原运行，以及结束后的回放。

接入中一并修正观测时间窗的迟到计数：按时接收后自然移出窗口的历史记录不再计为迟到；历史查询也不使用尚未接收的事件。
