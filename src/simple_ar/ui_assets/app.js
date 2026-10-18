/* No task lifecycle is persisted in the browser. Every refresh reads the owners. */
const $ = id => document.getElementById(id);
const deliveryPanel = document.createElement('article');
deliveryPanel.id = 'delivery';
$('approvalBox').after(deliveryPanel);
const token = location.hash.slice(1) || sessionStorage.getItem('sar-session') || '';
if (token) sessionStorage.setItem('sar-session', token);
history.replaceState(null, '', location.pathname);
let selected = '', snapshot = null, metadata = null, folder = '', previewURL = '', listingKey = '';
const pretty = value => JSON.stringify(value, null, 2);
function notice(message) { $('notice').textContent = message; $('notice').style.display = 'block'; }
async function api(path, data) {
  const response = await fetch(path, {method: data ? 'POST' : 'GET', headers: {
    Authorization: 'Bearer ' + token, ...(data ? {'Content-Type': 'application/json'} : {})
  }, ...(data ? {body: JSON.stringify(data)} : {})});
  if (!response.ok) { const error = await response.json(); throw Error(error.error); }
  return response;
}
async function json(path, data) { return (await api(path, data)).json(); }
async function action(op, extra = {}) {
  try {
    const result = await json('/api/action', {op, task: selected, ...extra});
    if (result.task && result.task !== selected) { selected = result.task; $('preview').replaceChildren(); $('files').replaceChildren(); }
    notice(result.message || '已记录，正在读取原任务状态');
    await refresh();
  } catch (error) { notice(error.message); }
}
function query(path, values) { return path + '?' + new URLSearchParams(values); }
function recipient() {
  const row = metadata.profiles.find(p => p.selector === $('profile').value);
  $('recipient').textContent = row ? Object.entries(row.recipients).map(([purpose, connection]) =>
    purpose + ' → ' + connection.model + ' · ' + connection.base_url).join('\n')
    : '没有可用的命名 Chat 配置；请先在服务端配置连接。';
}
function effectiveAccess() {
  const blocked = Number($('processes').value) === 0 || Number($('seconds').value) === 0;
  $('effectiveAccess').textContent = ($('execution').checked
    ? blocked ? '已允许提出执行，但进程次数或墙钟上限为 0，实际执行不可用；请调整额度或关闭权限。'
              : '可提出执行；实际运行仍需要逐项精确批准，并受所示进程上限约束。'
    : '进程额度不会授予执行权限；执行提议权限当前关闭。')
    + ' 图像权限与生成额度：' + pretty({allow_vision: metadata.creation_defaults.allow_vision,
                                       max_images: metadata.creation_defaults.max_images});
}
async function refresh() {
  const tasks = await json('/api/tasks');
  const nextListing = pretty({tasks, selected});
  // A polling read must not repeatedly destroy the user's keyboard focus.
  if (listingKey !== nextListing) {
    listingKey = nextListing;
    $('tasks').replaceChildren();
    for (const row of tasks) {
      const button = document.createElement('button');
      button.textContent = row.topic.slice(0, 140) + (row.topic.length > 140 ? '…' : '') + '\n· ' + row.status;
      button.title = row.topic;
      button.className = row.task === selected ? 'active' : '';
      button.onclick = async () => { selected = row.task; folder = ''; $('preview').replaceChildren(); $('files').replaceChildren(); await refresh(); await files(); };
      $('tasks').append(button);
    }
  }
  $('create').hidden = !!selected; $('detail').hidden = !selected;
  if (!selected) return;
  const task = selected;
  const next = await json(query('/api/task', {task}));
  if (selected !== task) return;
  snapshot = next;
  $('mode').textContent = snapshot.mode === 'live execution' ? '实时执行 · 此进程' : '已保存成果浏览';
  $('identity').textContent = snapshot.manifest.session_id;
  $('title').textContent = snapshot.manifest.topic.slice(0, 160) + (snapshot.manifest.topic.length > 160 ? '…' : '');
  $('originalGoal').textContent = snapshot.manifest.topic;
  $('status').textContent = snapshot.manifest.status;
  $('reason').textContent = snapshot.manifest.status_reason || '尚无状态说明';
  $('plan').textContent = snapshot.plan ? pretty(snapshot.plan) : '当前请求尚无已保存计划。计划是意图，不是已验证结果。';
  $('pending').textContent = pretty({current_attempt: snapshot.manifest.current_attempt,
    model: snapshot.model, access: snapshot.access, requests: snapshot.requests,
    pending: snapshot.pending, stop_requested: snapshot.stop_requested, worker_error: snapshot.worker_error});
  $('resources').textContent = pretty(snapshot.resources);
  $('budgetSummary').textContent = Object.entries(snapshot.resources.limits).map(([key, limit]) =>
    key + '：剩余 ' + snapshot.resources.remaining[key] + ' / 当前授权额度 ' + limit).join('\n')
    + '\n未知用量维度：' + (snapshot.resources.unknown_dimensions.join('、') || '无')
    + '\n未知预留未被释放。续额后的额度窗口不等于历次累计耗用；全部历史与授权理由保留在原始账本。';
  $('approvalBox').hidden = !snapshot.approval;
  $('proposal').textContent = pretty(snapshot.approval);
  $('resume').disabled = snapshot.worker_live;
  $('stop').disabled = !snapshot.worker_live;
  $('approve').disabled = snapshot.worker_live;
  const delivery = await json(query('/api/delivery', {task}));
  if (selected !== task) return;
  $('delivery').replaceChildren();
  const heading = document.createElement('h3'); heading.textContent = '交付与复用';
  $('delivery').append(heading);
  const label = document.createElement('p');
  label.textContent = delivery.label + (delivery.summary ? ' · ' + delivery.summary : ' · 尚无交付');
  $('delivery').append(label);
  for (const row of delivery.files) {
    const button = document.createElement('button'); button.textContent = row.group + ' · ' + row.name;
    button.onclick = () => previewFile({task, mount: row.mount}, row);
    $('delivery').append(button);
  }
  const oldMount = $('mount').value;
  $('mount').replaceChildren();
  for (const mount of ['workspace', 'evidence', ...snapshot.sources]) {
    const option = document.createElement('option'); option.value = mount;
    option.textContent = mount + (mount === 'evidence' ? ' · 已留存来源' : mount === 'workspace' ? ' · 工作文件' : ' · 只读材料');
    $('mount').append(option);
  }
  if ([...$('mount').options].some(o => o.value === oldMount)) $('mount').value = oldMount;
  else $('mount').value = snapshot.sources[0] || 'evidence';
}
async function files() {
  try {
    const values = {task: selected, mount: $('mount').value, path: folder};
    const rows = await json(query('/api/files', values));
    if (selected !== values.task || $('mount').value !== values.mount) return;
    $('files').replaceChildren(); $('location').textContent = folder || '/';
    for (const row of rows) {
      const button = document.createElement('button'); button.textContent = (row.directory ? '▸ ' : '') + row.name;
      button.onclick = async () => {
        if (row.directory) { folder = row.path; await files(); return; }
        await previewFile(values, row);
      };
      $('files').append(button);
    }
  } catch (error) { notice(error.message); }
}
async function previewFile(values, row) {
  try {
    const response = await api(query('/api/file', {...values, path: row.path}));
    const blob = await response.blob();
    if (selected !== values.task) return;
    if (previewURL) URL.revokeObjectURL(previewURL);
    previewURL = URL.createObjectURL(blob); $('preview').replaceChildren();
    const link = document.createElement('a'); link.href = previewURL; link.download = row.name;
    link.textContent = '下载 ' + row.name; $('preview').append(link);
    if (blob.type.startsWith('image/')) { const image = document.createElement('img'); image.src = previewURL; image.alt = row.name; $('preview').append(image); }
    else if (blob.type.startsWith('text/')) { const pre = document.createElement('pre'); pre.textContent = await blob.text(); $('preview').append(pre); }
    else { const label = document.createElement('p'); label.textContent = '该格式请下载查看（PDF 不嵌入活动文档）。'; $('preview').append(label); }
  } catch (error) { notice(error.message); }
}
$('new').onclick = () => { selected = ''; $('mode').textContent = 'Local workspace'; refresh().catch(e => notice(e.message)); };
$('profile').onchange = recipient;
$('createForm').onsubmit = async event => {
  event.preventDefault();
  effectiveAccess();
  const data = {goal: $('goal').value, materials: $('materials').value.split('\n').map(p => p.trim()).filter(Boolean),
    model: $('profile').value, binding_id: metadata.profiles.find(p => p.selector === $('profile').value)?.binding_id,
    max_turns: Number($('turns').value), max_tokens: Number($('tokens').value),
    process_invocations: Number($('processes').value), process_wall_seconds: Number($('seconds').value),
    network: $('network').checked, execution: $('execution').checked};
  if (confirm('确认材料只读访问、连接接收方和实际硬上限：\n' + pretty(data) + '\n' + $('effectiveAccess').textContent + '\n' + $('recipient').textContent)) {
    await action('create', {...data, confirmed: true}); await files();
  }
};
$('resume').onclick = () => action('resume');
$('stop').onclick = () => action('stop');
$('approve').onclick = () => { if (snapshot?.task === selected && snapshot.approval && confirm('批准以下精确操作？\n' + pretty(snapshot.approval))) action('approve', {token: snapshot.approval.token}); };
$('feedbackForm').onsubmit = async event => { event.preventDefault(); await action('feedback', {feedback: $('feedback').value}); };
$('switch').onclick = () => { const model = $('switchProfile').value; const recipient = metadata.profiles.find(p => p.selector === model); if (recipient && confirm('确认 task/code/writing 接收方：\n' + pretty(recipient))) action('switch', {model, binding_id: recipient.binding_id, reason: $('switchReason').value, switch_id: crypto.randomUUID(), confirmed: true}); };
$('mount').onchange = () => { folder = ''; files(); };
$('root').onclick = () => { folder = ''; files(); };
for (const id of ['processes', 'seconds', 'execution']) $(id).oninput = () => { if (metadata) effectiveAccess(); };
(async () => {
  try {
    metadata = await json('/api/meta'); $('grants').textContent = '项目：' + metadata.project + '\n只读授权：' + metadata.reads.join('；') + '\n任务：' + metadata.tasks;
    for (const [id, field] of [['turns', 'max_turns'], ['tokens', 'max_tokens'],
                             ['processes', 'process_invocations'], ['seconds', 'process_wall_seconds']]) {
      $(id).value = metadata.creation_defaults[field];
    }
    $('network').checked = metadata.creation_defaults.allow_network;
    $('execution').checked = metadata.creation_defaults.allow_execution;
    effectiveAccess(); $('createSubmit').disabled = false;
    for (const id of ['profile', 'switchProfile']) {
      const placeholder = document.createElement('option'); placeholder.value = '';
      placeholder.textContent = '选择明确的命名连接'; $(id).append(placeholder);
      for (const p of metadata.profiles) {
        const option = document.createElement('option'); option.value = p.selector;
        option.textContent = p.selector + ' · ' + p.model; $(id).append(option);
      }
    }
    $('profile').value = metadata.profiles.find(p => p.default)?.selector || '';
    recipient(); await refresh();
    setInterval(() => refresh().catch(e => notice(e.message)), 3000);
  } catch (error) { notice(error.message + '。请使用本次启动终端打印的完整链接。'); }
})();
