"""Small local FSRCNN models; bounded tiles, unchanged aspect and source bytes."""
import hashlib
import io
import math
import os
import threading
from pathlib import Path

from PIL import Image,ImageOps

MODEL_DIR=Path(__file__).resolve().parents[1]/'data/upscale'
MODEL_HASHES={2:'366b33f0084c7b3f2bf6724f0a2c77bca94fcec9d7b6d72389d330073b380d5c',
              4:'5c68d18db561aed8ead4ffedf1b897ea615baaf60ebf6c35f8e641f8fa4a21bf'}
_lock=threading.RLock()
_models={}


def target_size(width,height):
    if min(width,height)>=1000:return width,height
    ratio=1000/min(width,height)
    size=(math.ceil(width*ratio),math.ceil(height*ratio))
    if max(size)>5000 or size[0]*size[1]>25_000_000:
        raise ValueError('Обложка слишком вытянута: увеличение короткой стороны до 1000 пикселей даст слишком большое изображение.')
    return size


def _neural(image,scale):
    import cv2
    import numpy as np
    with _lock:
        if scale not in _models:
            path=MODEL_DIR/f'FSRCNN_x{scale}.pb'
            if hashlib.sha256(path.read_bytes()).hexdigest()!=MODEL_HASHES[scale]:
                raise ValueError('Модель увеличения обложек повреждена. Восстановите файлы data/upscale.')
            cv2.setNumThreads(max(1,min(4,os.cpu_count() or 1)))
            model=cv2.dnn_superres.DnnSuperResImpl_create()
            model.readModel(str(path));model.setModel('fsrcnn',scale)
            model.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            model.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
            _models[scale]=model
        source=np.ascontiguousarray(np.asarray(image.convert('RGB'))[:,:,::-1])
        h,w=source.shape[:2]
        output=np.empty((h*scale,w*scale,3),dtype=np.uint8)
        # Halo keeps tile boundaries out of the convolution's padding area.
        tile,halo=192,16
        for y in range(0,h,tile):
            for x in range(0,w,tile):
                right,bottom=min(w,x+tile),min(h,y+tile)
                left_pad,top_pad=max(0,x-halo),max(0,y-halo)
                crop=source[top_pad:min(h,bottom+halo),left_pad:min(w,right+halo)]
                up=_models[scale].upsample(crop)
                sx,sy=(x-left_pad)*scale,(y-top_pad)*scale
                output[y*scale:bottom*scale,x*scale:right*scale]=up[sy:sy+(bottom-y)*scale,sx:sx+(right-x)*scale]
        return Image.fromarray(output[:,:,::-1])


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
        scale=2 if min(width,height)>=500 else 4
        # A very small source still has only its original information; finish
        # the remaining scale with Lanczos rather than repeated neural passes.
        if size==image.size:
            enlarged=image.convert('RGB');method='Обрезка 1:1'
        else:
            try:
                enlarged=_neural(image,scale);method='FSRCNN'
            except (ImportError,FileNotFoundError):
                enlarged=image.convert('RGB');method='Lanczos'
        enlarged=enlarged.resize(size,Image.Resampling.LANCZOS)
        if alpha is not None:enlarged.putalpha(alpha.resize(size,Image.Resampling.LANCZOS))
        output=io.BytesIO()
        if alpha is not None:enlarged.save(output,'PNG',optimize=True)
        else:enlarged.save(output,'JPEG',quality=95,subsampling=0,optimize=True)
    result=output.getvalue();image_info(result)
    return result,{'method':method,'original_width':original_width,'original_height':original_height,
                   'width':size[0],'height':size[1],**({'crop':crop} if crop else {})}
