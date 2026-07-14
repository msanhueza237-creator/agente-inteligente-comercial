// Cascading region -> comuna select. Expects:
//   <script type="application/json" id="region-comuna-data">{...}</script>
//   <select id="region">...</select>
//   <select id="comuna">...</select>
// with the comuna <select> initially containing only "Todas"/placeholder.
(function () {
  const dataEl = document.getElementById("region-comuna-data");
  const regionEl = document.getElementById("region");
  const comunaEl = document.getElementById("comuna");
  if (!dataEl || !regionEl || !comunaEl) return;

  const regionComunaMap = JSON.parse(dataEl.textContent);
  const placeholder = comunaEl.dataset.placeholder || "";
  const preselected = comunaEl.dataset.selected || "";

  function populate(region, selected) {
    const comunas = regionComunaMap[region] || [];
    comunaEl.innerHTML = "";
    const emptyOption = document.createElement("option");
    emptyOption.value = "";
    emptyOption.textContent = placeholder;
    comunaEl.appendChild(emptyOption);
    for (const comuna of comunas) {
      const opt = document.createElement("option");
      opt.value = comuna;
      opt.textContent = comuna;
      if (comuna === selected) opt.selected = true;
      comunaEl.appendChild(opt);
    }
    comunaEl.disabled = comunas.length === 0;
  }

  regionEl.addEventListener("change", () => populate(regionEl.value, ""));

  if (regionEl.value) populate(regionEl.value, preselected);
})();
