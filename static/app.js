// helpers básicos
const els = {
  startCam: document.getElementById("startCam"),
  stopCam: document.getElementById("stopCam"),
  video: document.getElementById("video"),
  camSupport: document.getElementById("camSupport"),
  textInput: document.getElementById("textInput"),
  saveText: document.getElementById("saveText"),
  upload: document.getElementById("upload"),
  sendFile: document.getElementById("sendFile"),
  last: document.getElementById("lastText"),
  itemsBody: document.getElementById("itemsBody"),
  storeName: document.getElementById("storeName"),
  storeCNPJ: document.getElementById("storeCNPJ"),
  storeDate: document.getElementById("storeDate"),
};

function log(ev, data) {
  // console.log(ev, data);
}

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  return r.json();
}

async function renderScanDetails(scanId){
  try{
    const data = await fetchJSON(`/scan/${scanId}`);
    const s = data.scan || {};
    els.storeName.textContent = s.store_name || "—";
    els.storeCNPJ.textContent = s.cnpj || "—";
    els.storeDate.textContent = s.purchase_date || "—";
    els.itemsBody.innerHTML = "";
    (data.items || []).forEach(it=>{
      const tr = document.createElement("tr");
      const td1 = document.createElement("td"); td1.textContent = it.name || "";
      const td2 = document.createElement("td"); td2.textContent = it.qty ?? "";
      const td3 = document.createElement("td"); td3.textContent = it.unit_price ?? "";
      const td4 = document.createElement("td"); td4.textContent = it.total_price ?? "";
      tr.append(td1, td2, td3, td4);
      els.itemsBody.appendChild(tr);
    });
    log("scan_details", {scanId, items: (data.items||[]).length});
  }catch(e){
    log("scan_details_error", String(e));
  }
}

// salvar texto manual (ou vindo do scan da câmera)
async function saveText(text) {
  try {
    const json = await postJSON("/save_text", { text, source: "pwa" });
    els.last.textContent = text || "—";
    if (json && json.scan_id) { await renderScanDetails(json.scan_id); }
  } catch (e) {
    log("save_text_error", String(e));
  }
}

els.saveText.onclick = async () => {
  const t = els.textInput.value.trim();
  if (!t) { alert("Cole um texto ou URL de NFC-e."); return; }
  await saveText(t);
};

// upload de imagem
els.sendFile.onclick = async () => {
  const file = els.upload.files[0];
  if (!file) { alert("Selecione uma imagem."); return; }
  const fd = new FormData();
  fd.append("file", file, file.name);
  try {
    const resp = await fetch("/scan?source=pwa-upload", { method: "POST", body: fd });
    const json = await resp.json();
    log("scan", json);
    if (json.items && json.items[0]) {
      els.last.textContent = json.items[0].text || json.items[0];
      const sid = json.items[0].scan_id;
      if(sid){ await renderScanDetails(sid); }
    } else {
      alert("Nenhum QR encontrado na imagem.");
    }
  } catch (e) { log("scan_error", String(e)); }
};

// câmera com BarcodeDetector
let stream = null;
let scanning = false;
els.stopCam.onclick = async () => {
  scanning = false;
  if (stream) {
    stream.getTracks().forEach(t => t.stop());
    stream = null;
  }
};

els.startCam.onclick = async () => {
  if (!("BarcodeDetector" in window)) {
    els.camSupport.textContent = "Seu navegador não suporta BarcodeDetector. Use upload de imagem.";
    return;
  }
  const formats = await BarcodeDetector.getSupportedFormats();
  if (!formats.includes("qr_code")) {
    els.camSupport.textContent = "BarcodeDetector disponível, mas sem suporte a QR.";
    return;
  }
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
    els.video.srcObject = stream;
    const detector = new BarcodeDetector({ formats: ["qr_code"] });
    scanning = true;
    const tick = async () => {
      if (!scanning) return;
      try {
        const codes = await detector.detect(els.video);
        if (codes && codes.length) {
          const text = codes[0].rawValue || codes[0].rawValue?.trim();
          if (text) {
            scanning = false;
            els.stopCam.click(); // para a câmera
            await saveText(text);
            return;
          }
        }
      } catch (e) {
        log("detect_error", String(e));
      }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  } catch (e) {
    els.camSupport.textContent = "Não foi possível abrir a câmera. Tente o upload de imagem.";
  }
};

// PWA SW
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(()=>{});
}
