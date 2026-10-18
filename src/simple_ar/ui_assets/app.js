/* No task lifecycle is persisted in the browser. Every refresh reads the owners. */
const $ = id => document.getElementById(id);
const deliveryPanel = document.createElement('article');
deliveryPanel.id = 'delivery';
$('approvalBox').after(deliveryPanel);
const token = location.hash.slice(1) || sessionStorage.getItem('sar-session') || '';
if (token) sessionStorage.setItem('sar-session', token);
history.replaceState(null, '', location.pathname);
let selected = '', snapshot = null, metadata = null, folder = '', previewURL = '', listingKey = '', activityKey = '';
let confirmation = null, confirmationSending = false;
const pretty = value => JSON.stringify(value, null, 2);
const toolLabels = {acquire_source: '获取来源', search_sources: '检索来源', find_source_passages: '查阅原文',
  read_file: '读取文件', list_directory: '浏览材料', write_file: '保存文件', ask_user: '请求用户意见',
  update_work_plan: '更新可选计划', deliver_result: '提交交付', run_process: '运行进程',
  load_tools: '选择工具', focus_work: '选择工作重点', research: '研究材料',
  compose_document: '撰写报告', review_document: '审阅报告', export_report: '导出出版工程',
  research_collect: '收集研究材料', edit_file: '修改文件', copy_file: '复制文件',
  analyze_measurements: '分析已有测量', inspect_images: '检查图像'};
const statusLabels = {ok: '工具返回成功', blocked: '工具拒绝 / 阻塞', error: '工具失败',
  not_found: '未找到', unavailable: '原回执不可读'};
const extractionLabel = status => ({parsed: '已提取', metadata_only: '仅有元数据',
  failed: '提取失败', unavailable: '不可用'}[status] || status || '未记录');
function paragraph(parent, text, className = '') {
  const node = document.createElement('p'); node.textContent = text; node.className = className;
  parent.append(node); return node;
}
function renderActivity() {
  $('worker').textContent = snapshot.worker_live
    ? snapshot.stop_requested ? '本界面工作线程仍在运行；已请求在行动边界停止。' : '本界面工作线程正在运行。'
    : '本界面没有运行中的工作线程；下方是保存记录，外部进程状态未知。';
  const pending = snapshot.pending;
  $('currentAction').replaceChildren();
  paragraph($('currentAction'), pending
    ? (pending.kind === 'model' ? '待处理：模型决策' : '待处理：' +
      (toolLabels[pending.call?.function?.name] || pending.call?.function?.name || '工具动作')) + ' · ' + pending.attempt
    : '原任务没有 pending 动作。');
  paragraph($('currentAction'), '保留提案共 ' + snapshot.queued_actions + ' 项（可包含当前工具动作）；提案不代表已执行或交付。', 'hint');
  if (pending?.receipt) paragraph($('currentAction'), '当前 attempt 保存状态：' + pending.receipt.status + '\n' +
    (pending.receipt.diagnostics || []).join('\n') + '\n回执：' + pending.receipt.record, 'receipt');
  if (snapshot.worker_error) paragraph($('currentAction'), snapshot.worker_error, 'receipt');
  const rows = snapshot.activity || [];
  const key = pretty({task: selected, rows, total: snapshot.activity_total});
  if (activityKey === key) return; // Preserve focus and open receipt details on polls.
  activityKey = key;
  $('activitySummary').textContent = '最近 ' + rows.length + ' / ' + snapshot.activity_total +
    ' 项已接受工具回执（新到旧，跨请求历史）；成功状态不认证科研质量或当前交付。';
  $('activity').replaceChildren();
  if (!rows.length) paragraph($('activity'), '尚无已接受工具回执。模型草稿与未执行提案不计入进度。');
  for (const row of rows) {
    const item = document.createElement('li'); item.className = 'activity-item';
    paragraph(item, (toolLabels[row.tool] || row.tool || '工具记录') + ' · ' + (statusLabels[row.status] || row.status));
    paragraph(item, '请求 ' + (row.request_count ?? '未记录') + ' · ' + (row.attempt || '历史内联回执') + ' · ' + row.call_id, 'hint');
    if (row.action_unavailable) paragraph(item, '原动作不可读；以下只展示已保存工具回执。', 'hint');
    if (row.summary) paragraph(item, row.summary);
    if (row.error) paragraph(item, row.error, 'receipt');
    for (const doc of row.observed?.documents || []) {
      paragraph(item, doc.title || doc.id, 'source-title');
      paragraph(item, 'URL：' + (doc.url || '未记录') + '\n提取：' + extractionLabel(doc.status), 'receipt');
    }
    if (Object.keys(row.arguments || {}).length) paragraph(item, '操作参数：' + pretty(row.arguments), 'receipt');
    if (Object.keys(row.observed || {}).length) {
      const details = document.createElement('details');
      const summary = document.createElement('summary'); summary.textContent = '工具保存的观察与来源状态';
      const pre = document.createElement('pre'); pre.textContent = pretty(row.observed);
      details.append(summary, pre); item.append(details);
    }
    const locators = [...new Set([...(row.artifacts || []), row.observed?.document_record,
      row.arguments?.document_record].filter(value => typeof value === 'string'))];
    for (const locator of locators) {
      // Only recognized public mounts can become inspection buttons. The
      // server rechecks authorization and file guards when clicked.
      const mount = ['workspace', 'evidence', ...snapshot.sources].find(m => locator.startsWith(m + '/'));
      if (mount) {
        const button = document.createElement('button'); button.className = 'secondary';
        button.textContent = (locator === row.observed?.document_record ? '查看来源包 · ' : '查看保存文件 · ') + locator.split('/').pop();
        button.title = locator;
        const task = selected;
        button.onclick = () => previewFile({task, mount}, {path: locator.slice(mount.length + 1), name: locator.split('/').pop()});
        item.append(button);
      } else paragraph(item, '保存定位：' + locator, 'hint');
    }
    paragraph(item, '原回执：' + row.record, 'hint');
    $('activity').append(item);
  }
}
function notice(message) { $('notice').textContent = message; $('notice').style.display = 'block'; }
async function api(path, data) {
  const response = await fetch(path, {method: data ? 'POST' : 'GET', headers: {
    Authorization: 'Bearer ' + token, ...(data ? {'Content-Type': 'application/json'} : {})
  }, ...(data ? {body: JSON.stringify(data)} : {})});
  if (!response.ok) { const error = await response.json(); throw Error(error.error); }
  return response;
}
async function json(path, data) { return (await api(path, data)).json(); }
async function action(op, extra = {}, task = selected) {
  try {
    const result = await json('/api/action', {op, task, ...extra});
    if (result.task && result.task !== selected) { selected = result.task; $('preview').replaceChildren(); $('files').replaceChildren(); }
    notice(result.message || '已记录，正在读取原任务状态');
    await refresh();
    return result;
  } catch (error) { notice(error.message); }
}
function confirmationControls() {
  const locked = !!confirmation;
  $('workspaceMain').inert = locked;
  $('createSubmit').disabled = locked || !metadata;
  $('approve').disabled = locked || !snapshot?.approval || !!snapshot?.worker_live;
  $('switch').disabled = locked || !selected || !!snapshot?.worker_live;
  $('confirmationSubmit').disabled = confirmationSending;
  $('confirmationCancel').disabled = confirmationSending;
}
function reviewAction(title, op, extra, facts) {
  if (confirmation) return; // Repeated entry clicks cannot replace a review.
  // Serialize once: polling, form edits and selection changes cannot alter
  // either the reviewed request or its target task. Nothing is persisted.
  confirmation = {request: JSON.stringify({op, task: selected, ...extra}), focus: document.activeElement};
  $('confirmationTitle').textContent = title;
  $('confirmationFacts').replaceChildren();
  for (const [label, value] of Object.entries(facts)) {
    const heading = document.createElement('h3'); heading.textContent = label;
    const pre = document.createElement('pre'); pre.textContent = typeof value === 'string' ? value : pretty(value);
    $('confirmationFacts').append(heading, pre);
  }
  $('confirmationState').textContent = '核对本页冻结的内容。取消不会发送操作；修改需取消后重新核对。';
  $('confirmationSubmit').textContent = title;
  $('confirmation').hidden = false;
  confirmationControls();
  $('confirmation').focus();
}
function closeConfirmation() {
  const focus = confirmation?.focus;
  confirmation = null;
  $('confirmation').hidden = true;
  $('confirmationFacts').replaceChildren();
  confirmationControls();
  if (focus?.isConnected && !focus.disabled) focus.focus();
}
$('confirmationCancel').onclick = () => { if (!confirmationSending) closeConfirmation(); };
$('confirmationSubmit').onclick = async () => {
  if (!confirmation || confirmationSending) return;
  const {op, task, ...extra} = JSON.parse(confirmation.request);
  confirmationSending = true;
  confirmationControls();
  $('confirmationState').textContent = '正在提交本页冻结的操作，请等待服务器回执。';
  try {
    const result = await action(op, extra, task);
    if (op === 'create' && result?.task === selected) await files();
  }
  finally {
    // Consume the review even on failure/unknown response: no automatic replay
    // and no second submit of the same frozen approval or creation request.
    confirmationSending = false;
    closeConfirmation();
  }
};
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
  $('mode').textContent = snapshot.mode === 'live execution' ? '实时执行 · 此进程' : '保存记录 · 此进程未运行';
  $('identity').textContent = snapshot.manifest.session_id;
  $('title').textContent = snapshot.manifest.topic.slice(0, 160) + (snapshot.manifest.topic.length > 160 ? '…' : '');
  $('originalGoal').textContent = snapshot.manifest.topic;
  $('status').textContent = snapshot.manifest.status;
  $('reason').textContent = snapshot.manifest.status_reason || '尚无状态说明';
  renderActivity();
  $('plan').textContent = snapshot.plan ? pretty(snapshot.plan) : '最近回执中没有当前请求的计划。计划可选，由模型编写；没有计划不表示没有进展。';
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
  confirmationControls();
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
    const target = {...values, path: row.path};
    // Inspection is explicit, not a recursive JSON scan during polling.
    const source = await json(query('/api/source-preview', target));
    const response = await api(query('/api/file', target));
    const blob = await response.blob();
    if (selected !== values.task) return;
    if (previewURL) URL.revokeObjectURL(previewURL);
    previewURL = URL.createObjectURL(blob); $('preview').replaceChildren();
    const link = document.createElement('a'); link.href = previewURL; link.download = row.name;
    link.textContent = '下载 ' + row.name; $('preview').append(link);
    if (source) {
      paragraph($('preview'), '声明的来源包 · ' + row.name, 'source-title');
      paragraph($('preview'), '以下为来源 owner 保存的身份与有限覆盖；不核实来源原始性、主张或穷尽阅读。', 'hint');
      for (const doc of source.documents) {
        paragraph($('preview'), doc.title || doc.id, 'source-title');
        paragraph($('preview'), 'URL：' + (doc.url || '未记录') + '\n提取状态：' + extractionLabel(doc.status), 'receipt');
      }
      paragraph($('preview'), '保留文本块：' + source.retained_chunks +
        (source.documents_truncated ? '；只展示前 20 项文档身份。' : '。') + ' 下方仅为有界摘录。');
      for (const chunk of source.retained_preview) {
        paragraph($('preview'), '文档 ' + chunk.document_id + ' · 文本块 ' + chunk.chunk_id +
          (chunk.page != null ? ' · 页 ' + chunk.page : '') +
          (chunk.line_start != null ? ' · 行 ' + chunk.line_start + '–' + chunk.line_end : '') +
          ' · 字符 ' + chunk.character_start + '–' + chunk.character_end + ' / ' + chunk.total_characters +
          (chunk.truncated ? '（已截取）' : '') + ' · 提取：' + chunk.extraction_status, 'hint');
        const pre = document.createElement('pre'); pre.textContent = chunk.text; $('preview').append(pre);
      }
      const details = document.createElement('details');
      const summary = document.createElement('summary'); summary.textContent = '原始来源包文本';
      const raw = document.createElement('pre'); raw.textContent = await blob.text();
      details.append(summary, raw); $('preview').append(details);
    }
    else if (['image/png', 'image/jpeg', 'image/webp'].includes(blob.type)) { const image = document.createElement('img'); image.src = previewURL; image.alt = row.name; $('preview').append(image); }
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
  const profile = metadata.profiles.find(p => p.selector === data.model);
  if (!profile) { notice('请先选择可用的命名连接。'); return; }
  reviewAction('确认配置并创建任务', 'create', {...data, confirmed: true}, {
    '目标与只读材料': {目标: data.goal, 材料: data.materials},
    '实际接收方（task / code / writing）': profile.recipients,
    '连接身份': {model: data.model, binding_id: data.binding_id},
    '实际硬上限': {模型物理请求: data.max_turns, 总tokens: data.max_tokens,
      进程次数: data.process_invocations, 进程墙钟秒: data.process_wall_seconds,
      图像请求: metadata.creation_defaults.max_images},
    '生效权限': $('effectiveAccess').textContent + '\n公共来源网络：' + (data.network ? '允许' : '关闭'),
    '操作范围': '只创建并保存任务；创建后选择“继续执行”才开始工作。'
  });
};
$('resume').onclick = () => action('resume');
$('stop').onclick = () => action('stop');
$('approve').onclick = () => {
  if (snapshot?.task !== selected || !snapshot.approval || snapshot.worker_live) return;
  reviewAction('批准本页精确操作', 'approve', {token: snapshot.approval.token}, {
    '任务与当前模型': {task: selected, session_id: snapshot.manifest.session_id, model: snapshot.model},
    '实际执行操作与批准身份': snapshot.approval,
    '当前授权额度（不会新增）': {limits: snapshot.resources.limits, remaining: snapshot.resources.remaining,
      unknown_dimensions: snapshot.resources.unknown_dimensions},
    '操作范围': '批准本页 token 绑定的操作并继续同一任务；服务器仍核对当前 pending 提案和原执行权限。'
  });
};
$('feedbackForm').onsubmit = async event => { event.preventDefault(); await action('feedback', {feedback: $('feedback').value}); };
$('switch').onclick = () => {
  const model = $('switchProfile').value;
  const profile = metadata.profiles.find(p => p.selector === model);
  if (snapshot?.task !== selected || snapshot.worker_live || !profile) { notice('请选择当前任务和可用的命名连接。'); return; }
  const data = {model, binding_id: profile.binding_id, reason: $('switchReason').value,
    switch_id: crypto.randomUUID(), confirmed: true};
  reviewAction('确认更换连接', 'switch', data, {
    '任务与原模型': {task: selected, session_id: snapshot.manifest.session_id, model: snapshot.model},
    '更换后的实际接收方（task / code / writing）': profile.recipients,
    '更换身份与原因': data,
    '当前授权额度（不会新增）': {limits: snapshot.resources.limits, remaining: snapshot.resources.remaining,
      unknown_dimensions: snapshot.resources.unknown_dimensions},
    '操作范围': '显式更换命名连接；保存预算、未知用量与历史，后续执行仍沿原任务。'
  });
};
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
