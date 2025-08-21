# qr_utils.py
import cv2
import numpy as np

def decode_qr_bytes(image_bytes: bytes) -> list[str]:
    arr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return []
    detector = cv2.QRCodeDetector()
    try:
        retval, decoded_info, _, _ = detector.detectAndDecodeMulti(img)
        if retval and decoded_info:
            uniq = []
            for t in decoded_info:
                if t and t not in uniq:
                    uniq.append(t)
            if uniq:
                return uniq
    except Exception:
        pass
    data, points, _ = detector.detectAndDecode(img)
    return [data] if points is not None and data else []
