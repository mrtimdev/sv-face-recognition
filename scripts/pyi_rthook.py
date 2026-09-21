"""PyInstaller runtime hook: patch face_recognition_models to find bundled
model files without pkg_resources, and ensure builtins like quit() exist."""
import builtins
import os
import sys


if not hasattr(builtins, "quit"):
    builtins.quit = lambda code=0: sys.exit(code)


def _patch_face_recognition_models():
    """face_recognition_models uses pkg_resources.resource_filename to locate
    .dat files.  In frozen builds pkg_resources is unavailable, so we monkey-
    patch the module to return paths relative to the PyInstaller data dir."""
    models_dir = os.path.join(sys._MEIPASS, "face_recognition_models", "models")
    if not os.path.isdir(models_dir):
        return

    import importlib
    try:
        mod = importlib.import_module("face_recognition_models")
    except ImportError:
        return

    def _pose_predictor_model_location():
        return os.path.join(models_dir, "shape_predictor_68_face_landmarks.dat")

    def _pose_predictor_five_point_model_location():
        return os.path.join(models_dir, "shape_predictor_5_face_landmarks.dat")

    def _face_recognition_model_location():
        return os.path.join(models_dir, "dlib_face_recognition_resnet_model_v1.dat")

    def _cnn_face_detector_model_location():
        return os.path.join(models_dir, "mmod_human_face_detector.dat")

    mod.pose_predictor_model_location = _pose_predictor_model_location
    mod.pose_predictor_five_point_model_location = _pose_predictor_five_point_model_location
    mod.face_recognition_model_location = _face_recognition_model_location
    mod.cnn_face_detector_model_location = _cnn_face_detector_model_location


_patch_face_recognition_models()
