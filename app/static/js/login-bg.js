/* ============================================================
   康伴 · 登录页视频背景行为（仅 index.html 引用）
   职责：自动播放兜底 / 减弱动效 / 待机功耗 / 失败静默降级
   铁律：任何分支不得抛未捕获异常、不得影响登录表单交互
   ============================================================ */
(function () {
  "use strict";

  var bg = document.querySelector(".login-bg");
  var video = bg ? bg.querySelector(".login-bg__video") : null;
  if (!bg || !video) return;

  // 降级：隐藏视频元素，露出 .login-bg 上的海报兜底层（不产生黑块）
  function toFallback() {
    bg.classList.add("is-fallback");
  }

  // 减弱动效：命中即不自动播放（CSS 同时隐藏视频），只显示海报
  var prefersReduced = false;
  try {
    prefersReduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch (e) { /* 老 WebView 无 matchMedia：按未减弱处理 */ }

  function tryPlay() {
    if (prefersReduced || bg.classList.contains("is-fallback")) return;
    var p;
    try {
      p = video.play();
    } catch (e) {
      toFallback();          // 老 WebView play() 直接抛异常：静默退海报
      return;
    }
    if (p && typeof p.catch === "function") {
      p.catch(function () {
        toFallback();        // 自动播放被 WebView 拦截（需用户手势）：静默退海报
      });
    }
  }

  // 视频加载失败（网络/解码）：降海报
  if (video.error) toFallback();
  video.addEventListener("error", toFallback);

  tryPlay();

  // 长者平板常亮待机：页面隐藏即暂停省电，回前台续播（同样要 catch）
  document.addEventListener("visibilitychange", function () {
    try {
      if (document.hidden) {
        if (!video.paused) video.pause();
      } else {
        tryPlay();
      }
    } catch (e) { /* 播控异常不影响页面 */ }
  });
})();
