(() => {
  const storageKey = "personal_site_theme";
  const select = document.querySelector("[data-theme-switcher]");
  const available = select ? Array.from(select.options).map((opt) => opt.value) : [];

  function applyTheme(value) {
    if (!value) return;
    document.documentElement.dataset.theme = value;
    if (select) {
      select.value = value;
    }
  }

  const saved = window.localStorage.getItem(storageKey);
  if (saved && available.includes(saved)) {
    applyTheme(saved);
  } else if (select && available.length) {
    applyTheme(select.value);
  }

  if (select) {
    select.addEventListener("change", (event) => {
      const next = event.target.value;
      applyTheme(next);
      window.localStorage.setItem(storageKey, next);
    });
  }
})();

(() => {
  const entryForm = document.getElementById("log-workout-form");
  const typeForm = document.getElementById("new-type-form");
  const sleepForm = document.getElementById("log-sleep-form");
  const caffeineForm = document.getElementById("log-caffeine-form");

  document.body.addEventListener("workoutEntrySaved", () => {
    if (!entryForm) return;
    entryForm.reset();
    if (window.workoutEntryForm && typeof window.workoutEntryForm.reset === "function") {
      window.workoutEntryForm.reset();
    }
  });

  document.body.addEventListener("workoutTypeSaved", () => {
    if (!typeForm) return;
    typeForm.reset();
    if (window.workoutsTypeEditor && typeof window.workoutsTypeEditor.reset === "function") {
      window.workoutsTypeEditor.reset();
    }
  });

  document.body.addEventListener("sleepEntrySaved", () => {
    if (!sleepForm) return;
    sleepForm.reset();
  });

  document.body.addEventListener("caffeineEntrySaved", () => {
    if (!caffeineForm) return;
    caffeineForm.reset();
  });

  const nutritionForm = document.getElementById("log-nutrition-form");
  const ingredientForm = document.getElementById("add-ingredient-form");
  const mealForm = document.getElementById("create-meal-form");

  document.body.addEventListener("nutritionLogSaved", () => {
    if (!nutritionForm) return;
    nutritionForm.reset();
    if (window.nutritionLogForm && typeof window.nutritionLogForm.reset === "function") {
      window.nutritionLogForm.reset();
    }
  });

  document.body.addEventListener("ingredientSaved", () => {
    if (!ingredientForm) return;
    ingredientForm.reset();
  });

  document.body.addEventListener("mealSaved", () => {
    if (!mealForm) return;
    mealForm.reset();
    if (window.mealForm && typeof window.mealForm.reset === "function") {
      window.mealForm.reset();
    }
  });
})();

// Hamburger menu toggle
(() => {
  const btn = document.querySelector(".nav-toggle");
  const links = document.querySelector(".nav-links");
  if (btn && links) {
    btn.addEventListener("click", () => {
      const open = links.classList.toggle("open");
      btn.setAttribute("aria-expanded", open);
    });
    // Close menu when a link is clicked
    links.querySelectorAll(".nav-link").forEach((link) => {
      link.addEventListener("click", () => {
        links.classList.remove("open");
        btn.setAttribute("aria-expanded", "false");
      });
    });
  }
})();

// Set date inputs to the client's local date.
// The server sets a default but it may differ if the server timezone ≠ device timezone.
(() => {
  const today = new Date();
  const yyyy = today.getFullYear();
  const mm = String(today.getMonth() + 1).padStart(2, "0");
  const dd = String(today.getDate()).padStart(2, "0");
  const localDate = yyyy + "-" + mm + "-" + dd;

  document.querySelectorAll('input[type="date"]').forEach((input) => {
    // Only override if the input has the server-set "today" value
    // (not user-edited or pre-filled for editing)
    if ("noAutodate" in input.dataset) return;
    if (input.value && input.value === input.defaultValue) {
      input.value = localDate;
    }
  });
})();

// Service worker registration
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/static/sw.js");
}
