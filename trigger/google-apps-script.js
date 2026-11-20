// 始祖鸟监控 · 定时触发器（Google Apps Script）
// GitHub 自带的定时经常延迟或跳过，这里改由 Google 每 10 分钟调用一次 GitHub 接口，手动触发监控。
// 令牌存在「项目设置 → 脚本属性」的 GH_TOKEN 里，不写在代码中。

const REPO = 'Zhangting-Ge/arc-monitor';
const WORKFLOW = 'monitor.yml';

function dispatch_(mode) {
  const token = PropertiesService.getScriptProperties().getProperty('GH_TOKEN');
  if (!token) throw new Error('还没有在脚本属性里设置 GH_TOKEN');
  const res = UrlFetchApp.fetch(
    'https://api.github.com/repos/' + REPO + '/actions/workflows/' + WORKFLOW + '/dispatches', {
      method: 'post',
      contentType: 'application/json',
      headers: {
        Authorization: 'Bearer ' + token,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
      },
      payload: JSON.stringify({ ref: 'main', inputs: { mode: mode } }),
      muteHttpExceptions: true,
    });
  const code = res.getResponseCode();
  console.log(mode + ' → HTTP ' + code + ' ' + res.getContentText().slice(0, 200));
  if (code !== 204 && code !== 200) {
    throw new Error('触发失败：HTTP ' + code);   // 抛错后 Google 会发邮件提醒
  }
}

function runCheck() { dispatch_('run'); }      // 每 10 分钟：检查网站
function runDigest() { dispatch_('digest'); }  // 每晚 20:00：日报

// 运行一次即可：清掉旧触发器，建立两个新的
function setupTriggers() {
  ScriptApp.getProjectTriggers().forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('runCheck').timeBased().everyMinutes(10).create();
  ScriptApp.newTrigger('runDigest').timeBased().everyDays(1).atHour(20).nearMinute(0)
    .inTimezone('Europe/Berlin').create();
  console.log('已建立：每 10 分钟检查一次 + 每晚 20:00 日报');
}
