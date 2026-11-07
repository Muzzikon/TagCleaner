# FSRCNN artwork upscaling

These small TensorFlow graphs are consumed locally by OpenCV `dnn_superres`.
Source and pinned commit: `manifest.json`. Runtime verifies the two SHA-256 digests.

Author's model repository: https://github.com/Saafke/FSRCNN_Tensorflow

OpenCV integration documentation: https://docs.opencv.org/4.12.0/d5/d29/tutorial_dnn_superres_upscale_image_single.html

No training code, TensorFlow runtime, cloud service, or audio data is used for inference.
If OpenCV or model files are unavailable, the application explicitly records Lanczos scaling instead.
