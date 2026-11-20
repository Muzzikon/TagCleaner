# Artwork upscaling models

OpenCV loads the TensorFlow graphs through `dnn_superres` and the Real-ESRGAN ONNX graph through `dnn`.
The sources, pinned commits and SHA-256 digests are recorded in `manifest.json`.

- FSRCNN: https://github.com/Saafke/FSRCNN_Tensorflow
- LapSRN: https://github.com/fannymonori/TF-LapSRN
- Real-ESRGAN General x4v3 ([upstream](https://github.com/xinntao/Real-ESRGAN), [ONNX export](https://huggingface.co/qualcomm/Real-ESRGAN-General-x4v3))

OpenCV integration documentation: https://docs.opencv.org/4.12.0/d5/d29/tutorial_dnn_superres_upscale_image_single.html

No training code, TensorFlow runtime, cloud service, or audio data is used for inference.
TagCleaner uses LapSRN for very small covers and FSRCNN for larger ones. For inputs from 64 pixels on the shorter side up to 250,000 pixels in area, it blends in Real-ESRGAN to reduce compression and blur while retaining lettering and fine edges. Missing weights leave the remaining local enlargement methods available.
