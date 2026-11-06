/* 公共加载协调：保持 workspace/reader 的传统脚本与全局函数兼容。 */
(() => {
  const markReady = () => {
    document.documentElement.dataset.uiReady = '1';
    window.dispatchEvent(new Event('rongke:ui-ready'));
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', markReady, {once:true});
  else markReady();
})();