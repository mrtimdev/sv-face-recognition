# Bundled recognition and landmark models

Downloaded 2026-09-23; original ONNX bytes retained. Local OpenCV 4.10 DNN
inference only. SHA-256 is checked before loading. Licenses accompany the files.

| Local file | Original model | License | SHA-256 |
| --- | --- | --- | --- |
| yunet.onnx | face_detection_yunet_2023mar.onnx | MIT, Copyright 2020 Shiqi Yu | 8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4 |
| sface.onnx | face_recognition_sface_2021dec.onnx | Apache-2.0 | 0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79 |
| face_mesh.onnx | face_mesh_Nx3x192x192.onnx | Apache-2.0 | 3ca77cf59c18e4da0eccb46695bf604683fa564253e3385892981a5c274fb10f |

Sources:

- [OpenCV YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet)
- [OpenCV SFace](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface), contributed by Yaoyao Zhong; ONNX conversion credited upstream to Chengrui Wang.
- [Face Mesh ONNX](https://github.com/yakhyo/mediapipe-face-mesh-onnx), Copyright 2026 Yakhyokhuja Valikhujaev; reconstructed/exported from Google's MediaPipe Face Mesh. The application's OpenCV adapter uses its documented ROI and input/output conventions.

Download URLs:

- https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
- https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx
- https://github.com/yakhyo/mediapipe-face-mesh-onnx/releases/download/weights/face_mesh_Nx3x192x192.onnx

YuNet receives BGR, returns boxes and five alignment points. SFace aligns the
full-resolution BGR face, then produces a normalized 128-element identity
vector. Identity comparison uses cosine distance. Its templates are distinct
from dlib templates even though their dimensions match.

Face Mesh receives a roll-normalized square crop, 1.5 times the larger face-box
dimension, resized to 192x192 RGB float32 in [0,1]. Its 468 points are in crop
pixels, mapped back by the inverse affine transform. The presence output is a
logit (zero is probability 0.5). Six-point eyelid contours and lip contours feed
the existing adaptive blink-or-smile checker.

These models detect, recognize, and track faces; they do not certify liveness.
The separate MiniFASNet PAD gate remains mandatory for attendance. Recorded
expressions can pass Face Mesh's expression check. Deployment must validate
genuine-user acceptance and physical screen/print rejection on its own camera.
