const els = {
  video: document.getElementById("video"),
  start: document.getElementById("start"),
  stop: document.getElementById("stop"),
  output: document.getElementById("output"),
  upload: document.getElementById("upload"),
  sendFile: document.getElementById("sendFile"),
  last: document.getElementById("last"),
};
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => { navigator.serviceWorker.register("/sw.js"); });
}
let stream = null, rafId = null, scanning = false;
const supported = "BarcodeDetector" in window;
async function startScan() {
  if (!supported) { alert("BarcodeDetector não suportado. Use upload."); return; }
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
    els.video.srcObject = stream;
    await els.video.play();
    scanning = true;
    const detector = new BarcodeDetector({ formats: ["qr_code"] });
    loop(detector);
    els.start.disabled = true; els.stop.disabled = false;
  } catch (e) { console.error(e); alert("Não foi possível acessar a câmera."); }
}
function stopScan() {
  scanning = false;
  if (rafId) cancelAnimationFrame(rafId);
  if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
  els.start.disabled = false; els.stop.disabled = true;
}
async function loop(detector) {
  if (!scanning) return;
  try {
    const codes = await detector.detect(els.video);
    if (codes && codes.length) {
      const text = codes[0].rawValue || codes[0].rawValue;
      els.last.textContent = text;
      await saveText(text);
      await new Promise(r => setTimeout(r, 1200));
    }
  } catch (e) { console.warn(e); }
  rafId = requestAnimationFrame(() => loop(detector));
}
async function saveText(text) {
  try {
    const resp = await fetch("/save_text", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ text, source: "pwa" }),
    });
    const json = await resp.json();
    log("save_text", json);
  } catch (e) { log("save_text_error", String(e)); }
}
els.sendFile.onclick = async () => {
  const file = els.upload.files[0];
  if (!file) { alert("Selecione uma imagem."); return; }
  const fd = new FormData();
  fd.append("file", file, file.name);
  try {
    const resp = await fetch("/scan?source=pwa-upload", { method: "POST", body: fd });
    const json = await resp.json();
    log("scan", json);
    if (json.items && json.items[0]) els.last.textContent = json.items[0];
  } catch (e) { log("scan_error", String(e)); }
};
function log(tag, payload) {
  const time = new Date().toISOString();
  const line = `${time} [${tag}] ${typeof payload === "string" ? payload : JSON.stringify(payload)}`;
  const pre = document.getElementById("output"); pre.textContent = line + "\n" + pre.textContent;
}
document.getElementById("start").addEventListener("click", startScan);
document.getElementById("stop").addEventListener("click", stopScan);
