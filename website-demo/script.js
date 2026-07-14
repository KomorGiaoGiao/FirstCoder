const copyButton = document.querySelector(".copy");

copyButton?.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText("pipx install firstcoder");
    copyButton.textContent = "已复制 ✓";
  } catch {
    copyButton.textContent = "复制失败";
  }
  window.setTimeout(() => { copyButton.textContent = "复制"; }, 1400);
});

const reveals = document.querySelectorAll(".reveal");
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

if (reduceMotion.matches || !("IntersectionObserver" in window)) {
  reveals.forEach((item) => item.classList.add("visible"));
} else {
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        entry.target.classList.add("visible");
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.12 });
  reveals.forEach((item) => observer.observe(item));
}
