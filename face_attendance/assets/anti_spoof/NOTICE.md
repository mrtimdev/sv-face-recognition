# Bundled anti-spoof models

MiniFASNetV2 and MiniFASNetV1SE originate from Minivision's
[Silent-Face-Anti-Spoofing](https://github.com/minivision-ai/Silent-Face-Anti-Spoofing).
Copyright Minivision and upstream contributors. Distributed under Apache-2.0;
the accompanying LICENSE is included with these models.

ONNX exports downloaded on 2026-09-23 from
[yakhyo/face-anti-spoofing](https://github.com/yakhyo/face-anti-spoofing/releases/tag/weights):

| File | Crop scale | SHA-256 |
| --- | --- | --- |
| MiniFASNetV2.onnx | 2.7 | b32929adc2d9c34b9486f8c4c7bc97c1b69bc0ea9befefc380e4faae4e463907 |
| MiniFASNetV1SE.onnx | 4.0 | ebab7f90c7833fbccd46d3a555410e78d969db5438e169b6524be444862b3676 |

Download URLs:
- https://github.com/yakhyo/face-anti-spoofing/releases/download/weights/MiniFASNetV2.onnx
- https://github.com/yakhyo/face-anti-spoofing/releases/download/weights/MiniFASNetV1SE.onnx

Models are unmodified. The app's inference adapter uses existing OpenCV DNN
with raw BGR float32 inputs (1,3,80,80), softmax logits and live class index 1.
Crop expansion follows Minivision's original edge-shifting geometry. Both
models must score at least 0.80; this is an application policy, not a calibrated
probability or independently certified operating point. Three consecutive
passing samples spanning at least 0.35 seconds are required; evidence expires
in 0.75 seconds. Scores are never a substitute for the identity/expression gates.
The model files are hash-checked before loading. No runtime download or upload.

## Validation and limits

Local OpenCV 4.10.0 CPU smoke check, using the current YuNet face boxes:
- Upstream image_T1.jpg: conservative live score 0.9998 (accepted).
- Upstream image_F1.jpg: 0.0180 (rejected).
- Upstream image_F2.jpg: 0.0005 (rejected).
- Earlier dlib-detector check of the supplied original selfie MOV: seven sampled face frames scored above 0.9999.
  This file contains the source recording, not a camera recapture of a screen,
  and is NOT a successful replay-rejection test.

These examples validate loading and basic preprocessing only. They are not a
representative accuracy evaluation. Camera optics, lighting, face scale,
display quality and attack type can change results. Both false acceptance and
false rejection remain possible. The actual camera view of a phone replay
and genuine users on the deployment camera must be tested. Video injected
straight into the camera stream is outside this RGB presentation check's scope.
