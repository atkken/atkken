# 电话音频解码（DTMF / FSK / CDR / 录音保存）

这个项目提供一个可落地的起点：

- 从电话音频（WAV）中提取 **DTMF 按键**。
- 尝试解码 **FSK（Bell 202 Caller ID）** 获取来电号码。
- 保存录音副本并生成 **CDR JSON**。

> 适合你描述的场景：声卡采集电话线路音频，自动提取号码与业务元数据。

## 1. 快速开始

```bash
python3 telephony_decoder.py --input your_call.wav --cdr cdr.json --recording-dir recordings
```

执行后会：

1. 解析 `your_call.wav`（16-bit PCM）。
2. 输出识别到的 DTMF 串。
3. 尝试从 FSK Caller ID 中提取号码。
4. 将音频复制到 `recordings/call_时间戳.wav`。
5. 生成 `cdr.json`。

## 2. 声卡直接录音（可选）

如果安装了 `sounddevice`：

```bash
pip install sounddevice
python3 telephony_decoder.py --record-seconds 20 --input captured.wav
```

## 3. CDR 输出示例

```json
{
  "timestamp": "2026-01-01 12:00:00",
  "audio_file": "recordings/call_20260101_120000.wav",
  "duration_sec": 12.37,
  "dtmf_digits": "13800138000#",
  "fsk_phone_numbers": ["02188886666"],
  "fsk_payload_hex": "..."
}
```

## 4. 工程建议（生产环境）

- 采集链路建议固定到 8k/16bit/单声道，避免重采样误差。
- DTMF 建议加入前后静音门限与幅度归一化。
- FSK（来显）建议在首振铃后窗口做专门检测（提升成功率）。
- 对 CDR 增加唯一通话 ID、主叫被叫、开始结束时间、状态码等字段。
- 录音建议按日期分目录并配合对象存储归档。

## 5. 限制说明

- 当前 FSK 解码是通用实现，面对低信噪比线路或设备回声场景，可能需要更严格的滤波与时钟恢复。
- 输入音频目前仅支持 16-bit PCM WAV。
