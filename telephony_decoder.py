#!/usr/bin/env python3
"""电话音频解码工具：从 WAV 中提取 DTMF、FSK(Caller ID) 并生成 CDR。"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import struct
import time
import wave
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, List, Optional


DTMF_LOW_FREQS = [697, 770, 852, 941]
DTMF_HIGH_FREQS = [1209, 1336, 1477, 1633]
DTMF_KEYS = {
    (697, 1209): "1",
    (697, 1336): "2",
    (697, 1477): "3",
    (697, 1633): "A",
    (770, 1209): "4",
    (770, 1336): "5",
    (770, 1477): "6",
    (770, 1633): "B",
    (852, 1209): "7",
    (852, 1336): "8",
    (852, 1477): "9",
    (852, 1633): "C",
    (941, 1209): "*",
    (941, 1336): "0",
    (941, 1477): "#",
    (941, 1633): "D",
}


@dataclass
class CDRRecord:
    timestamp: str
    audio_file: str
    duration_sec: float
    dtmf_digits: str
    fsk_phone_numbers: List[str]
    fsk_payload_hex: str


def _read_wav_mono_16bit(path: str) -> tuple[int, list[float]]:
    with wave.open(path, "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        nframes = wf.getnframes()
        if sample_width != 2:
            raise ValueError("仅支持 16-bit PCM WAV")
        raw = wf.readframes(nframes)
    samples = struct.unpack("<" + "h" * (len(raw) // 2), raw)
    if channels == 2:
        mono = [(samples[i] + samples[i + 1]) / 2.0 for i in range(0, len(samples), 2)]
    else:
        mono = [float(v) for v in samples]
    return sample_rate, mono


def _write_wav_mono_16bit(path: str, sample_rate: int, samples: Iterable[float]) -> None:
    out = bytearray()
    for s in samples:
        s = max(-32768, min(32767, int(s)))
        out += struct.pack("<h", s)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(out))


def _goertzel_power(samples: list[float], sample_rate: int, target_freq: float) -> float:
    n = len(samples)
    if n == 0:
        return 0.0
    k = int(0.5 + (n * target_freq) / sample_rate)
    omega = (2.0 * math.pi * k) / n
    coeff = 2.0 * math.cos(omega)
    q0 = 0.0
    q1 = 0.0
    q2 = 0.0
    for sample in samples:
        q0 = coeff * q1 - q2 + sample
        q2 = q1
        q1 = q0
    return q1 * q1 + q2 * q2 - coeff * q1 * q2


def decode_dtmf(samples: list[float], sample_rate: int) -> str:
    frame_ms = 40
    frame_size = int(sample_rate * frame_ms / 1000)
    if frame_size <= 0:
        return ""

    detected: list[str] = []
    tone_frames_needed = max(1, int(80 / frame_ms))
    gap_frames_needed = max(1, int(40 / frame_ms))

    current_tone: Optional[str] = None
    tone_count = 0
    gap_count = 0

    for i in range(0, len(samples) - frame_size, frame_size):
        frame = samples[i : i + frame_size]
        low_powers = {f: _goertzel_power(frame, sample_rate, f) for f in DTMF_LOW_FREQS}
        high_powers = {f: _goertzel_power(frame, sample_rate, f) for f in DTMF_HIGH_FREQS}

        low_freq = max(low_powers, key=low_powers.get)
        high_freq = max(high_powers, key=high_powers.get)

        sorted_low = sorted(low_powers.values(), reverse=True)
        sorted_high = sorted(high_powers.values(), reverse=True)
        low_ok = sorted_low[0] > 2.5 * (sorted_low[1] + 1e-9)
        high_ok = sorted_high[0] > 2.5 * (sorted_high[1] + 1e-9)

        key = DTMF_KEYS.get((low_freq, high_freq)) if (low_ok and high_ok) else None

        if key is None:
            gap_count += 1
            if gap_count >= gap_frames_needed:
                current_tone = None
                tone_count = 0
            continue

        gap_count = 0
        if current_tone == key:
            tone_count += 1
        else:
            current_tone = key
            tone_count = 1

        if tone_count == tone_frames_needed:
            detected.append(key)

    return "".join(detected)


def _bits_to_bytes(bits: list[int]) -> bytes:
    out = []
    i = 0
    while i + 10 <= len(bits):
        chunk = bits[i : i + 10]
        i += 10
        if chunk[0] != 0 or chunk[9] != 1:
            continue
        value = 0
        for bit_index in range(8):
            value |= (chunk[1 + bit_index] & 1) << bit_index
        out.append(value)
    return bytes(out)


def _decode_fsk_bytes_with_offset(samples: list[float], sample_rate: int, offset: int) -> bytes:
    spb = sample_rate / 1200.0
    bits: list[int] = []
    pos = float(offset)
    while int(pos + spb) < len(samples):
        segment = samples[int(pos) : int(pos + spb)]
        p_mark = _goertzel_power(segment, sample_rate, 1200)
        p_space = _goertzel_power(segment, sample_rate, 2200)
        bits.append(1 if p_mark >= p_space else 0)
        pos += spb
    return _bits_to_bytes(bits)


def decode_fsk_bell202(samples: list[float], sample_rate: int) -> bytes:
    if sample_rate < 4000:
        return b""
    best = b""
    spb_i = max(1, int(sample_rate / 1200))
    for offset in range(0, min(spb_i, 80)):
        candidate = _decode_fsk_bytes_with_offset(samples, sample_rate, offset)
        if len(candidate) > len(best):
            best = candidate
    return best


def parse_callerid_numbers(payload: bytes) -> List[str]:
    numbers: List[str] = []

    # 常见 CID 消息类型：0x04(SDMF), 0x80(MDMF)
    for i in range(len(payload) - 2):
        msg_type = payload[i]
        if msg_type not in (0x04, 0x80):
            continue
        msg_len = payload[i + 1]
        end = i + 2 + msg_len
        if end > len(payload):
            continue
        msg = payload[i + 2 : end]

        # 校验和：type+len+body+checksum == 0 mod 256
        if end < len(payload):
            checksum = payload[end]
            if ((sum(payload[i:end]) + checksum) & 0xFF) != 0:
                pass

        if msg_type == 0x04 and len(msg) >= 8:
            # SDMF: 前8字节时间戳，后面通常是号码
            tail = msg[8:]
            number = "".join(chr(c) for c in tail if 32 <= c < 127)
            if number and any(ch.isdigit() for ch in number):
                numbers.append(number)
        elif msg_type == 0x80:
            j = 0
            while j + 2 <= len(msg):
                param_type = msg[j]
                param_len = msg[j + 1]
                param_val = msg[j + 2 : j + 2 + param_len]
                if len(param_val) < param_len:
                    break
                if param_type == 0x02:  # calling number
                    number = "".join(chr(c) for c in param_val if 32 <= c < 127)
                    if number and any(ch.isdigit() for ch in number):
                        numbers.append(number)
                j += 2 + param_len

    return sorted(set(numbers))


def maybe_record_from_soundcard(output_wav: str, duration_sec: int, sample_rate: int = 8000) -> None:
    try:
        import sounddevice as sd  # type: ignore
    except Exception as exc:
        raise RuntimeError("未安装 sounddevice，无法直接从声卡录音。") from exc

    print(f"开始录音 {duration_sec}s -> {output_wav}")
    rec = sd.rec(int(duration_sec * sample_rate), samplerate=sample_rate, channels=1, dtype="int16")
    sd.wait()
    mono = [float(x[0]) for x in rec.tolist()]
    _write_wav_mono_16bit(output_wav, sample_rate, mono)


def build_cdr(input_wav: str, recording_dir: str, cdr_path: str) -> CDRRecord:
    sr, samples = _read_wav_mono_16bit(input_wav)
    dtmf = decode_dtmf(samples, sr)
    fsk_raw = decode_fsk_bell202(samples, sr)
    numbers = parse_callerid_numbers(fsk_raw)

    Path(recording_dir).mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    saved_recording = os.path.join(recording_dir, f"call_{stamp}.wav")
    shutil.copy2(input_wav, saved_recording)

    rec = CDRRecord(
        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
        audio_file=saved_recording,
        duration_sec=round(len(samples) / sr, 3),
        dtmf_digits=dtmf,
        fsk_phone_numbers=numbers,
        fsk_payload_hex=fsk_raw.hex(),
    )

    with open(cdr_path, "w", encoding="utf-8") as f:
        json.dump(asdict(rec), f, ensure_ascii=False, indent=2)

    return rec


def main() -> None:
    parser = argparse.ArgumentParser(description="电话音频解码：DTMF / FSK + CDR + 录音归档")
    parser.add_argument("--input", help="输入 WAV 文件（16-bit PCM）")
    parser.add_argument("--record-seconds", type=int, help="直接从声卡录音 N 秒并作为输入")
    parser.add_argument("--recording-dir", default="recordings", help="录音归档目录")
    parser.add_argument("--cdr", default="cdr.json", help="CDR 输出 JSON 路径")
    args = parser.parse_args()

    input_wav = args.input
    if args.record_seconds:
        input_wav = input_wav or "captured.wav"
        maybe_record_from_soundcard(input_wav, args.record_seconds)

    if not input_wav:
        raise SystemExit("请提供 --input 或 --record-seconds")

    rec = build_cdr(input_wav, args.recording_dir, args.cdr)
    print(json.dumps(asdict(rec), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
