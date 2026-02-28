const toastEl = document.getElementById("toast");
const modalEl = document.getElementById("modal");

function toast(msg){
  toastEl.textContent = msg;
  toastEl.classList.add("show");
  window.clearTimeout(toastEl._t);
  toastEl._t = window.setTimeout(() => toastEl.classList.remove("show"), 1600);
}

async function apiAction(name){
  const res = await fetch("/api/action", {
    method:"POST",
    headers:{ "Content-Type":"application/json" },
    body: JSON.stringify({ name })
  });
  const data = await res.json().catch(()=> ({}));
  if(!res.ok || !data.ok){
    throw new Error(data.error || `Request failed (${res.status})`);
  }
  return data;
}

function openModal(){ modalEl.setAttribute("aria-hidden", "false"); }
function closeModal(){ modalEl.setAttribute("aria-hidden", "true"); }

modalEl.addEventListener("click", (e) => {
  if(e.target.classList.contains("modal-backdrop")) closeModal();
});

document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-action]");
  if(!btn) return;
  const action = btn.getAttribute("data-action");

  try{
    if(action === "power_menu"){ openModal(); return; }
    if(action === "cancel"){ closeModal(); return; }

    if(action === "reboot"){
      closeModal(); toast("Rebooting…");
      await apiAction("reboot"); return;
    }
    if(action === "poweroff"){
      closeModal(); toast("Powering off…");
      await apiAction("poweroff"); return;
    }

    if(action === "camera_demo"){
      toast("Launching Live Demo…");
      await apiAction("camera_demo"); return;
    }
    if(action === "video_demo"){
      toast("Starting Video Demo…");
      await apiAction("video_demo"); return;
    }
    if(action === "return_desktop"){
      toast("Returning to Pi…");
      await apiAction("return_desktop"); return;
    }
  }catch(err){
    toast(`Error: ${err.message}`);
  }
});

document.addEventListener("keydown", (e) => {
  if(e.key === "Escape") closeModal();
});
