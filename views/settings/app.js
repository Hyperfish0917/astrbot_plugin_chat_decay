const bridge = window.AstrBotPluginView;
const output = document.getElementById("result");
const rate = document.getElementById("reset-rates");
const all = document.getElementById("reset-all");
await bridge.ready();
async function reset(kind, confirmText) {
  if (!window.confirm(confirmText)) return;
  rate.disabled = true;
  all.disabled = true;
  output.textContent = "正在保存…";
  try {
    const result = await bridge.apiPost(`settings/${kind}`, {});
    output.textContent = result.message || "设置已保存，请重新打开插件配置确认。";
  } catch (error) {
    output.textContent = `重置失败：${error.message}`;
  } finally {
    rate.disabled = false;
    all.disabled = false;
  }
}
rate.addEventListener("click", () => reset("reset-rates", "确认将四项概率恢复为 20%、100%、80%、50%？"));
all.addEventListener("click", () => reset("reset-all", "确认恢复全部默认配置？自定义关键词也会被清除。"));
