(function attachDiagnosticsRouting(root) {
  function itemId(item) {
    return String(item?.id || item?.check_id || item?.ref_id || '');
  }

  function itemTab(item) {
    return item?.action_target?.tab || item?.target_tab || item?.tab || '';
  }

  function isModelDiagnosticTarget(item = {}) {
    const id = itemId(item);
    const tab = itemTab(item);
    return tab === 'models' || id.startsWith('models.') || id === 'model_runtime';
  }

  function isSkillMcpDiagnosticTarget(item = {}) {
    const id = itemId(item);
    const tab = itemTab(item);
    return ['skills', 'mcp', 'marketplace'].includes(tab)
      || id.startsWith('skills.')
      || id.startsWith('mcp.')
      || id.startsWith('skill_mcp')
      || ['skill_mcp', 'install_flow', 'local_tools', 'network_tools', 'network.search'].includes(id);
  }

  function diagnosticSkillMcpTarget(item = {}) {
    const id = itemId(item);
    const tab = itemTab(item);
    if (tab === 'skills' || id.startsWith('skills.')) return 'skills';
    if (tab === 'mcp' || id.startsWith('mcp.') || id === 'network.search' || ['local_tools', 'network_tools'].includes(id)) return 'mcp';
    return 'marketplace';
  }

  function diagnosticMcpPreference(item = {}) {
    const id = itemId(item);
    const tab = itemTab(item);
    if (id === 'network.search' || id === 'network.capability' || id === 'network_tools' || /search|联网|网络/.test(`${id} ${tab}`)) {
      return { id: 'web-search', tool: 'search', keywords: ['search', '联网', '检索'] };
    }
    if (id.startsWith('document.') || id === 'documents') {
      return { id: 'report', tool: 'generate_document', keywords: ['report', 'document', '文档'] };
    }
    return { id: '', tool: '', keywords: [] };
  }

  function serverTools(server) {
    return Array.isArray(server?.tools) ? server.tools : [];
  }

  function matchesMcpPreferenceId(server, preference) {
    return Boolean(preference?.id) && String(server?.id || '') === preference.id;
  }

  function matchesMcpPreferenceTool(server, preference) {
    return Boolean(preference?.tool) && serverTools(server).some((tool) => String(tool?.name || '') === preference.tool);
  }

  function matchesMcpPreferenceKeyword(server, preference) {
    const keywords = Array.isArray(preference?.keywords) ? preference.keywords : [];
    if (!keywords.length) return false;
    const text = `${server?.id || ''} ${server?.name || ''} ${server?.description || ''}`.toLowerCase();
    const tools = serverTools(server);
    return keywords.some((keyword) => {
      const value = String(keyword).toLowerCase();
      return text.includes(value)
        || tools.some((tool) => `${tool?.name || ''} ${tool?.description || ''}`.toLowerCase().includes(value));
    });
  }

  function firstMatch(servers, predicate) {
    return servers.find((server) => server?.enabled && predicate(server))
      || servers.find((server) => predicate(server))
      || null;
  }

  function chooseDiagnosticMcpServer(servers = [], source = {}) {
    const items = Array.isArray(servers) ? servers : [];
    const preference = diagnosticMcpPreference(source);
    return firstMatch(items, (server) => matchesMcpPreferenceId(server, preference))
      || firstMatch(items, (server) => matchesMcpPreferenceTool(server, preference))
      || firstMatch(items, (server) => matchesMcpPreferenceKeyword(server, preference))
      || (preference.id || preference.tool ? null : items.find((server) => server?.enabled) || items[0] || null);
  }

  function diagnosticRouteTarget(item = {}) {
    const id = itemId(item);
    const tab = itemTab(item);
    if (isModelDiagnosticTarget(item)) return 'models';
    if (isSkillMcpDiagnosticTarget(item)) return 'skill_mcp';
    if (tab === 'knowledge' || id.startsWith('knowledge.') || id === 'knowledge') return 'knowledge';
    if (tab === 'artifacts' || id.startsWith('artifacts.') || id.startsWith('document.') || id === 'documents') return 'artifacts';
    if (tab === 'experts' || id.startsWith('expert.') || id === 'expert_mode') return 'experts';
    if (tab === 'memory' || id.startsWith('memory.') || id === 'memory_context') return 'memory';
    if (tab === 'loops' || id.startsWith('automation.') || id === 'automation') return 'loops';
    if (tab === 'workspaces' || id.startsWith('workspace.')) return 'workspaces';
    if (id.startsWith('security.') || id.startsWith('database.')) return 'diagnostics';
    if (tab === 'chat' || id.startsWith('runtime.') || id === 'file.upload_context' || ['file_input', 'runtime_recovery', 'core_platform'].includes(id)) return 'chat';
    return tab || '';
  }

  const api = {
    isModelDiagnosticTarget,
    isSkillMcpDiagnosticTarget,
    diagnosticSkillMcpTarget,
    diagnosticMcpPreference,
    chooseDiagnosticMcpServer,
    diagnosticRouteTarget,
  };

  if (root) root.AgentNexusDiagnosticsRouting = api;
  if (typeof document !== 'undefined' && document.defaultView) document.defaultView.AgentNexusDiagnosticsRouting = api;
  if (typeof window !== 'undefined') window.AgentNexusDiagnosticsRouting = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : typeof window !== 'undefined' ? window : {});
