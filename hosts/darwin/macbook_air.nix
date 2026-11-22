{ ... }:

{
  imports = [ ../default.nix ];

  # `envy config refine` writes non-sensitive machine values here.
  # Add machine-specific envy.* overrides here.

  # BEGIN ENVY MANAGED CONFIG
  # `envy config` updates only this block. Other machine policy stays intact.

  # --- AMUX CONFIG ---
  envy.amux.skipPermissions = true;

  # --- BASE CONFIG ---
  envy.user.name = "chi";
  envy.user.home = "/Users/chi";

  # --- DARWIN_SERVICES CONFIG ---
  envy.darwin.services.mihomo.mode = "none";
  envy.darwin.services.mihomo.tun = false;
  envy.darwin.services.openssh.mode = "keep";

  # --- ENV CONFIG ---
  envy.repository.path = "/Users/chi/.envy";

  # --- GIT CONFIG ---
  envy.git.name = "Kie-Chi";
  envy.git.email = "137579437@qq.com";

  # --- HABITS CONFIG ---
  envy.habits.terminalScratchpad.gesture = "F12";
  envy.habits.globalLauncher.gesture = "Option+Space";

  # --- MIRRORS CONFIG ---
  envy.mirrors.mode = "china";

  # --- STORAGE CONFIG ---
  envy.storage.mode = "manual";
  envy.storage.maintenance.go-modcache = false;
  envy.storage.archive.repository = "sftp:xikai@192.168.255.139:/vol1/1000/share/envY/archive/macbook_air";
  envy.storage.archive.mountSource = "";

  # --- VSCODE CONFIG ---
  envy.vscode.mode = "remote";

  # Shared rules are inherited; native maintenance is explicit and never scheduled.
  envy.storage.maintenance = {
    nix = true;
    docker = true;
    go = true;
    uv = true;
    npm = true;
    pip = true;
    brew = true;
  };
  # Shared rules are provided by modules/storage; only machine differences belong here.
  # END ENVY MANAGED CONFIG

  # BEGIN ENVY MANAGED SOFTWARE
  # `envy setup` and `envy software` own only these machine-local selections.

  envy.darwin.software.homebrew.casks.include = [
    "steam"
    "t3-code"
  ];

  envy.darwin.software.homebrew.casks.exclude = [
    "trae"
  ];

  envy.darwin.software.homebrew.formulae.include = [
    "hexo"
    "shiquda/cmux-pocket/cmux-pocket"
  ];

  envy.darwin.software.homebrew.repositories.include = [
    "shiquda/cmux-pocket"
  ];

  envy.software.pypi.tools.exclude = [
    "headroom"
  ];
  # END ENVY MANAGED SOFTWARE

  # BEGIN ENVY MANAGED SOURCES
  envy.software.sources = builtins.fromJSON "{\"homebrew:shiquda/cmux-pocket\":{\"ecosystem\":\"homebrew\",\"label\":\"cmux Pocket\",\"owner\":\"machine\",\"parameters\":{\"tap\":\"shiquda/cmux-pocket\",\"trustedFormulae\":[\"cmux-pocket\"]},\"platforms\":[\"darwin\"],\"scope\":\"system\",\"type\":\"tap\"}}";
  # END ENVY MANAGED SOURCES

  # BEGIN ENVY MANAGED AGENTS
  # envY owns agent selections, endpoint metadata, and credential references here.

  # --- LLM CREDENTIALS ---
  envy.llm.credentials.deepseek.kind = "api-key";
  envy.llm.credentials.deepseek.secretPath = "llm/deepseek/apikey";
  envy.llm.credentials.hbapi.kind = "api-key";
  envy.llm.credentials.hbapi.secretPath = "llm/hbapi/apikey";
  envy.llm.credentials.nisl.secretPath = "llm/nisl/apikey";
  envy.llm.credentials.stepfun.kind = "api-key";
  envy.llm.credentials.stepfun.secretPath = "llm/steps/apikey";
  envy.llm.credentials.xikai.kind = "api-key";
  envy.llm.credentials.xikai.secretPath = "llm/xikai/apikey";

  # --- LLM ENDPOINTS ---
  envy.llm.endpoints.deepseek-openai.baseUrl = "https://api.deepseek.com";
  envy.llm.endpoints.deepseek-openai.protocol = "openai-compatible";
  envy.llm.endpoints.stepfun-anthropic.baseUrl = "https://stepfun.com";
  envy.llm.endpoints.stepfun-anthropic.protocol = "anthropic-compatible";
  envy.llm.endpoints.stepfun-openai.baseUrl = "https://stepfun.com/v1";
  envy.llm.endpoints.stepfun-openai.protocol = "openai-compatible";
  envy.llm.endpoints.sub2api-anthropic.baseUrl = "https://49.233.149.230:3389";
  envy.llm.endpoints.sub2api-anthropic.protocol = "anthropic-compatible";
  envy.llm.endpoints.xikai-openai.baseUrl = "https://api.xikai.site/v1";
  envy.llm.endpoints.xikai-openai.protocol = "openai-compatible";
  envy.llm.endpoints.nisl-openai.baseUrl = "https://cn.api.xikai.site:34443/v1";
  envy.llm.endpoints.nisl-openai.protocol = "openai-compatible";

  # --- AGENT PROFILES ---
  envy.agentProfiles.deepseek.api = "openai-completions";
  envy.agentProfiles.deepseek.credential = "deepseek";
  envy.agentProfiles.deepseek.endpoint = "deepseek-openai";
  envy.agentProfiles.deepseek.model = "deepseek-v4-pro";
  envy.agentProfiles.hb-sub2api.api = "anthropic-messages";
  envy.agentProfiles.hb-sub2api.credential = "hbapi";
  envy.agentProfiles.hb-sub2api.endpoint = "sub2api-anthropic";
  envy.agentProfiles.hb-sub2api.model = "claude-opus-5";
  envy.agentProfiles.xikai-magpie.api = "openai-responses";
  envy.agentProfiles.xikai-magpie.contextWindow = 272000;
  envy.agentProfiles.xikai-magpie.credential = "xikai";
  envy.agentProfiles.xikai-magpie.endpoint = "xikai-openai";
  envy.agentProfiles.xikai-magpie.maxTokens = 128000;
  envy.agentProfiles.xikai-magpie.model = "gpt-6.1-sol";
  envy.agentProfiles.nisl.api = "openai-responses";
  envy.agentProfiles.nisl.credential = "nisl";
  envy.agentProfiles.nisl.endpoint = "nisl-openai";
  envy.agentProfiles.nisl.model = "deepseek-v4.1-flash";

  # --- AGENTS ---
  envy.agents.cc.adapter = "claude-code";
  envy.agents.cc.aliases = [ "ccli" ];
  envy.agents.cc.credential = "hbapi";
  envy.agents.cc.endpoint = "sub2api-anthropic";
  envy.agents.cc.model = "claude-opus-5";
  envy.agents.codex.profile = "xikai-magpie";
  envy.agents.codex.models = [
    { profile = "xikai-magpie"; model = "gpt-6.1-sol"; }
    { profile = "xikai-magpie"; model = "gpt-6-astra"; }
  ];
  envy.agents.codex.adapter = "codex";
  envy.agents.codex.credential = "xikai";
  envy.agents.codex.endpoint = "xikai-openai";
  envy.agents.codex.model = "gpt-6.1-sol";
  envy.agents.pi.adapter = "pi";
  envy.agents.pi.models = [
    { profile = "xikai-magpie"; model = "gpt-6.2-sol"; }
    { profile = "xikai-magpie"; model = "gpt-6-astra"; }
    { profile = "nisl"; model = "deepseek-v4.1-flash"; }
    { profile = "deepseek"; model = "deepseek-flash"; }
  ];
  envy.agents.pi.additionalSettings = builtins.fromJSON "{\"compaction\": {\"keepRecentTokens\": 27200, \"reserveTokens\": 40960}, \"defaultTools\": [\"+codemode\"]}";
  envy.agents.pi.api = "openai-responses";
  envy.agents.pi.contextWindow = 272000;
  envy.agents.pi.credential = "xikai";
  envy.agents.pi.endpoint = "xikai-openai";
  envy.agents.pi.maxTokens = 128000;
  envy.agents.pi.model = "gpt-6.1-sol";
  envy.agents.pi.moreThinking = true;
  # END ENVY MANAGED AGENTS

}
