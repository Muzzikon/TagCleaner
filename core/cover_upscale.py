"""Local cover restoration and enlargement with preserved aspect ratio."""
import hashlib
import io
import math
import os
import threading
from pathlib import Path

from PIL import Image,ImageFilter,ImageOps

MODEL_DIR=Path(__file__).resolve().parents[1]/'data/upscale'
PIPELINE_REVISION = 3
RESTORE_MODEL = 'Real-ESRGAN-General-x4v3.onnx'
RESTORE_RESIDUAL_LAYER = 'onnx_node!/model/body.66/Conv'
RESTORE_MAX_PIXELS = 250_000
MODEL_HASHES = {
    ('FSRCNN', 2): '366b33f0084c7b3f2bf6724f0a2c77bca94fcec9d7b6d72389d330073b380d5c',
    ('FSRCNN', 4): '5c68d18db561aed8ead4ffedf1b897ea615baaf60ebf6c35f8e641f8fa4a21bf',
    ('LapSRN', 4): 'd3e95c93cafae5ce5a8ed57ce9abf07f2de58da8c5d6d656b766774969835ee2',
    RESTORE_MODEL: '1f8ecd7af904762c1cbc7f29d43101e8c74fa1fd6355a309ef569d5df64bf9a9',
}
_lock=threading.RLock()
_models={}


def target_size(width,height):
    short_side = min(width, height)
    if short_side >= 1000:
        return width, height
    size = tuple((side * 1000 + short_side - 1) // short_side for side in (width, height))
    if max(size)>5000 or size[0]*size[1]>25_000_000:
        raise ValueError('Обложка слишком вытянута: увеличение короткой стороны до 1000 пикселей даст слишком большое изображение.')
    return size


def _neural(image,scale,method='FSRCNN'):
    import cv2
    import numpy as np
    key = (method, scale)
    with _lock:
        if key not in _models:
            path=MODEL_DIR/f'{method}_x{scale}.pb'
            if hashlib.sha256(path.read_bytes()).hexdigest()!=MODEL_HASHES[key]:
                raise ValueError('Модель увеличения обложек повреждена. Восстановите файлы data/upscale.')
            cv2.setNumThreads(max(1,min(4,os.cpu_count() or 1)))
            model=cv2.dnn_superres.DnnSuperResImpl_create()
            model.readModel(str(path));model.setModel(method.lower(),scale)
            model.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            model.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
            _models[key]=model
        source=np.ascontiguousarray(np.asarray(image.convert('RGB'))[:,:,::-1])
        h,w=source.shape[:2]
        output=np.empty((h*scale,w*scale,3),dtype=np.uint8)
        # Halo keeps tile boundaries out of the convolution's padding area.
        tile,halo=(320,32) if method=='LapSRN' else (192,16)
        for y in range(0,h,tile):
            for x in range(0,w,tile):
                right,bottom=min(w,x+tile),min(h,y+tile)
                left_pad,top_pad=max(0,x-halo),max(0,y-halo)
                crop=source[top_pad:min(h,bottom+halo),left_pad:min(w,right+halo)]
                up=_models[key].upsample(crop)
                sx,sy=(x-left_pad)*scale,(y-top_pad)*scale
                output[y*scale:bottom*scale,x*scale:right*scale]=up[sy:sy+(bottom-y)*scale,sx:sx+(right-x)*scale]
        return Image.fromarray(output[:,:,::-1])


def _enlarge(image):
    width, height = image.size
    if min(width, height) <= 320 and width * height <= 128_000:
        try:
            return _neural(image, 4, 'LapSRN'), 'LapSRN'
        except (ImportError, FileNotFoundError):
            pass
    scale = 2 if min(width, height) >= 500 else 4
    try:
        return _neural(image, scale), 'FSRCNN'
    except (ImportError, FileNotFoundError):
        return image.convert('RGB'), 'Lanczos'


def _restore(image):
    import cv2
    import numpy as np

    with _lock:
        if RESTORE_MODEL not in _models:
            path = MODEL_DIR / RESTORE_MODEL
            weights = path.read_bytes()
            if hashlib.sha256(weights).hexdigest() != MODEL_HASHES[RESTORE_MODEL]:
                raise ValueError('Модель восстановления обложек повреждена. Восстановите файлы data/upscale.')
            cv2.setNumThreads(max(1, min(4, os.cpu_count() or 1)))
            model = cv2.dnn.readNetFromONNX(np.frombuffer(weights, dtype=np.uint8))
            model.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            model.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
            _models[RESTORE_MODEL] = model
        model = _models[RESTORE_MODEL]
        source = cv2.dnn.blobFromImage(np.asarray(image.convert('RGB')), scalefactor=1 / 255)
        model.setInput(source)
        residual = model.forward(RESTORE_RESIDUAL_LAYER)[0]
        width, height = image.size
        # dnn_superres registers a different DepthToSpace channel order in OpenCV.
        # Match the ONNX graph's CRD pixel shuffle and nearest-neighbor residual addition.
        pixels = residual.reshape(3, 4, 4, height, width).transpose(0, 3, 1, 4, 2)
        output = (pixels + source[0, :, :, None, :, None]).reshape(3, height * 4, width * 4)
    rgb = np.rint(np.clip(output.transpose(1, 2, 0), 0, 1) * 255).astype(np.uint8)
    return Image.fromarray(rgb)


def upscale(blob,crop_square=False,crop_position=50):
    from core.covers import image_info
    if type(crop_square) is not bool or type(crop_position) not in (int,float) or not math.isfinite(crop_position) or not 0<=crop_position<=100:
        raise ValueError('Некорректная область обрезки.')
    image_info(blob)  # Decode limits and format validation also apply here.
    with Image.open(io.BytesIO(blob)) as source:
        image=ImageOps.exif_transpose(source)
        original_width,original_height=image.size
        crop=None
        if crop_square and original_width!=original_height:
            side=min(image.size)
            left=round((original_width-side)*crop_position/100);top=round((original_height-side)*crop_position/100)
            crop={'box':[left,top,left+side,top+side],'position':crop_position}
            image=image.crop(crop['box'])
        width,height=image.size;size=target_size(width,height)
        if size==image.size and crop is None:return blob,{'method':'unchanged','original_width':width,'original_height':height}
        alpha=image.convert('RGBA').getchannel('A') if image.mode in ('RGBA','LA','P') else None
        if size==image.size:
            enlarged=image.convert('RGB');method='Обрезка 1:1'
        else:
            enlarged,method=_enlarge(image)
        enlarged=enlarged.resize(size,Image.Resampling.LANCZOS)
        if method == 'LapSRN':
            enlarged = enlarged.filter(ImageFilter.UnsharpMask(radius=1.15, percent=85, threshold=3))
        if size != image.size and min(width, height) >= 64 and width * height <= RESTORE_MAX_PIXELS:
            try:
                restored = _restore(image)
            except (ImportError, FileNotFoundError):
                pass
            else:
                # Keep more of the original lettering and texture on larger inputs.
                strength = 0.65 if min(width, height) <= 320 else 0.45
                enlarged = Image.blend(enlarged, restored.resize(size, Image.Resampling.LANCZOS), strength)
                method = f'Real-ESRGAN + {method}'
        if alpha is not None:enlarged.putalpha(alpha.resize(size,Image.Resampling.LANCZOS))
        output=io.BytesIO()
        if alpha is not None:enlarged.save(output,'PNG',optimize=True)
        else:enlarged.save(output,'JPEG',quality=95,subsampling=0,optimize=True)
    result=output.getvalue();image_info(result)
    return result,{'method':method,'original_width':original_width,'original_height':original_height,
                   'width':size[0],'height':size[1],'pipeline_revision':PIPELINE_REVISION,
                   **({'crop':crop} if crop else {})}
