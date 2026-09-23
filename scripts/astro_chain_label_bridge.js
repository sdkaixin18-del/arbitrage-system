(() => {
  "use strict";

  const NOTES_KEY = "coin_pairs_notes";
  const APPLIED_KEY = "astro_auto_chain_labels_applied_v1";
  const REPAIRS_KEY = "astro_auto_chain_label_repairs_v1";
  const currentScriptUrl = document.currentScript?.src || window.location.href;
  const feedUrl = new URL("./auto-chain-labels.json", currentScriptUrl).href;

  const readJson = (key, fallback) => {
    try {
      const value = JSON.parse(window.localStorage.getItem(key) || "null");
      return value ?? fallback;
    } catch (_error) {
      return fallback;
    }
  };

  const syncLabels = async () => {
    try {
      const response = await window.fetch(`${feedUrl}?t=${Date.now()}`, {
        cache: "no-store",
        credentials: "same-origin",
      });
      if (!response.ok) return;
      const payload = await response.json();
      const items = payload && typeof payload.items === "object" ? payload.items : {};
      const notesValue = readJson(NOTES_KEY, []);
      // Remove only our exact PancakeSwap chain labels, retaining manual notes.
      const previousNotes = Array.isArray(notesValue) ? notesValue : [];
      const notes = previousNotes.filter(note => {
        const generated = items[String(note?.id || "")]?.text;
        return !(String(generated || "").startsWith("PancakeSwap V3链：") && note?.text === generated);
      });
      const appliedValue = readJson(APPLIED_KEY, []);
      const applied = new Set(Array.isArray(appliedValue) ? appliedValue.map(String) : []);
      const repairsValue = readJson(REPAIRS_KEY, {});
      const repairs = repairsValue && typeof repairsValue === "object" && !Array.isArray(repairsValue) ? repairsValue : {};
      // Empty placeholders are not visible labels.
      const existing = new Set(notes.filter(item => String(item?.text || "").trim()).map(item => String(item?.id || "")).filter(Boolean));
      let changed = notes.length !== previousNotes.length;

      for (const [rawId, item] of Object.entries(items)) {
        const id = String(rawId || "").trim();
        const text = String(item?.text || "").trim();
        const revision = String(item?.repairRevision || "");
        const repairRequested = revision && repairs[id] !== revision;
        if (!id || !text || text.startsWith("PancakeSwap V3链：") || (applied.has(id) && !repairRequested)) continue;
        // An existing user label always wins. Mark it as handled so clearing
        // it later does not cause the automatic label to reappear.
        if (!existing.has(id)) {
          // Replace an empty placeholder so Astro's first-id lookup sees text.
          for (let i = notes.length - 1; i >= 0; i--) {
            if (String(notes[i]?.id || "") === id) notes.splice(i, 1);
          }
          notes.unshift({ id, text, updatedAt: Number(item?.updatedAt) || Date.now() });
          existing.add(id);
          changed = true;
        }
        applied.add(id);
        if (repairRequested) repairs[id] = revision;
      }

      // Persist and verify notes before acknowledging delivery. If storage fails,
      // the next poll retries instead of permanently marking a missing label done.
      if (changed) {
        const serialized = JSON.stringify(notes);
        window.localStorage.setItem(NOTES_KEY, serialized);
        if (window.localStorage.getItem(NOTES_KEY) !== serialized) return;
      }
      window.localStorage.setItem(REPAIRS_KEY, JSON.stringify(repairs));
      window.localStorage.setItem(APPLIED_KEY, JSON.stringify(Array.from(applied).slice(-500)));
      if (!changed) return;
      // Astro reads its label store when the page mounts. One reload makes a
      // newly published label visible immediately; the applied set prevents a
      // reload loop and preserves later manual edits.
      window.location.reload();
    } catch (_error) {
      // Label synchronization is deliberately non-blocking. Trading and card
      // creation continue when the optional feed is temporarily unavailable.
    }
  };

  void syncLabels();
  window.setInterval(() => void syncLabels(), 3000);
})();
