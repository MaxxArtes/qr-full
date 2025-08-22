# qr_utils.py – decodifica QR de bytes usando OpenCV
import numpy as np
import cv2

def decode_qr_bytes(data: bytes):
    """Retorna lista de textos decodificados de QRs na imagem."""
    nparr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return []

    detector = cv2.QRCodeDetector()
    texts = []

    # OpenCV moderno possui detectAndDecodeMulti
    try:
        retval, decoded_info, points, _ = detector.detectAndDecodeMulti(img)
        if retval and decoded_info:
            for t in decoded_info:
                if t:
                    texts.append(t)
    except Exception:
        pass

    # fallback para single
    if not texts:
        try:
            t, _, _ = detector.detectAndDecode(img)
            if t:
                texts.append(t)
        except Exception:
            pass

    return texts
