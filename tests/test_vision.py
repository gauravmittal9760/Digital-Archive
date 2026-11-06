"""Checks the onnxruntime plumbing of services/vision_local.py with a tiny hand-built model
(mean RGB -> 1000 logits), so no big download is needed. The real MobileNetV2 file is used the same way."""
import io
import struct

import pytest

ort = pytest.importorskip("onnxruntime")
np = pytest.importorskip("numpy")


def _v(n):                       # protobuf varint
    out = b""
    while True:
        b = n & 0x7F; n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _f(num, wt, payload):
    return _v(num << 3 | wt) + payload


def _ld(num, data):
    return _f(num, 2, _v(len(data)) + data)


def _value_info(name, dims):
    shape = b"".join(_ld(1, _f(1, 0, _v(d))) for d in dims)
    tensor = _f(1, 0, _v(1)) + _ld(2, shape)
    return _ld(1, name.encode()) + _ld(2, _ld(1, tensor))


def _model_bytes(weights):       # weights: (3, 1000) float32
    init = b"".join(_f(1, 0, _v(d)) for d in weights.shape) + _f(2, 0, _v(1)) + _ld(8, b"W") + _ld(9, weights.astype("<f4").tobytes())
    nodes = [_ld(1, b"input") + _ld(2, b"pooled") + _ld(4, b"GlobalAveragePool"),
             _ld(1, b"pooled") + _ld(2, b"flat") + _ld(4, b"Flatten"),
             _ld(1, b"flat") + _ld(1, b"W") + _ld(2, b"output") + _ld(4, b"MatMul")]
    graph = b"".join(_ld(1, n) for n in nodes) + _ld(2, b"g") + _ld(5, init) + _ld(11, _value_info("input", [1, 3, 224, 224])) + _ld(12, _value_info("output", [1, 1000]))
    return _f(1, 0, _v(8)) + _ld(8, _ld(1, b"") + _f(2, 0, _v(13))) + _ld(7, graph)


def test_vision_pipeline_end_to_end(tmp_path, monkeypatch):
    from PIL import Image
    from services import vision_local as vl
    monkeypatch.delenv("DISABLE_LOCAL_VISION")
    w = np.zeros((3, 1000), dtype=np.float32); w[0, 291] = 10.0          # red-ish picture -> class 291 ("lion")
    (tmp_path / "m.onnx").write_bytes(_model_bytes(w))
    (tmp_path / "l.txt").write_text("\n".join(f"class{i}" if i != 291 else "lion" for i in range(1000)))
    monkeypatch.setattr(vl, "MODEL_FILE", str(tmp_path / "m.onnx")); monkeypatch.setattr(vl, "LABEL_FILE", str(tmp_path / "l.txt"))
    monkeypatch.setitem(vl._state, "session", None); monkeypatch.setitem(vl._state, "failed", False)
    buf = io.BytesIO(); Image.new("RGB", (640, 480), (220, 90, 40)).save(buf, "JPEG")
    result = vl.analyze(buf.getvalue())
    assert result and result["labels"][0] == "lion" and result["category"] == "pets"
    assert "animal" in result["labels"] and "big" in result["labels"]


def test_category_ranges():
    from services.vision_local import category_for_index as c
    assert c(291) == "pets" and c(323) == "pets" and c(963) == "food" and c(975) == "nature" and c(500) == "other"
