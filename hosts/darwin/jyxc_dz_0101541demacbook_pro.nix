{ pkgs, ... }:

{
  imports = [ ../default.nix ];

  # BEGIN ENVY MANAGED CONFIG
  # `envy config` updates only this block. Other machine policy stays intact.

  # --- AMUX CONFIG ---
  envy.amux.skipPermissions = true;

  # --- BASE CONFIG ---
  envy.user.name = "jyxc-dz-0101541";
  envy.user.home = "/Users/jyxc-dz-0101541";

  # --- DARWIN_SERVICES CONFIG ---
  envy.darwin.services.mihomo.mode = "none";
  envy.darwin.services.mihomo.tun = false;
  envy.darwin.services.openssh.mode = "keep";

  # --- ENV CONFIG ---
  envy.repository.path = "/Users/jyxc-dz-0101541/.envy";

  # --- GIT CONFIG ---
  envy.git.name = "Kie-Chi";
  envy.git.email = "137579437@qq.com";

  # --- HABITS CONFIG ---
  envy.habits.terminalScratchpad.gesture = "F12";
  envy.habits.globalLauncher.gesture = "Option+Space";

  # --- MIRRORS CONFIG ---
  envy.mirrors.mode = "china";

  # --- VSCODE CONFIG ---
  envy.vscode.mode = "remote";
  # END ENVY MANAGED CONFIG

  # BEGIN ENVY MANAGED MIRROR OVERRIDES
  # `envy mirror` owns this block. Other machine policy stays intact.
  envy.mirrors.overrides.npm.registry = "https://mirrors.cloud.tencent.com/npm/";
  envy.mirrors.overrides.npm.source = "chsrc:npm/tencent";
  # END ENVY MANAGED MIRROR OVERRIDES

  # BEGIN ENVY MANAGED SOFTWARE
  # `envy setup` and `envy software` own only these machine-local selections.

  envy.darwin.software.nix.systemPackages.include = [
    # envy: {"id":"sunshine","ref":"nix:sunshine","sourceId":"nix:nixpkgs"}
    pkgs."sunshine"
  ];
  envy.darwin.software.nix.systemPackages.references = builtins.fromJSON "{\"sunshine\":\"nix:sunshine\"}";

  envy.darwin.software.homebrew.casks.include = [
    "steam"
    "sunlogincontrol"
  ];

  envy.darwin.software.homebrew.casks.exclude = [
    "microsoft-remote-desktop"
    "ticktick"
    "trae"
    "moonlight"
    "dingtalk"
    "neteasemusic"
    "skim"
    "iina"
    "sunlogincontrol"
  ];

  envy.darwin.software.homebrew.formulae.include = [
    "maven"
    "pandoc"
    "tectonic"
  ];

  envy.software.pypi.tools.include = builtins.fromJSON "[{\"id\":\"browser-use\",\"name\":\"browser-use\",\"ref\":\"pypi:browser-use\"}]";
  # END ENVY MANAGED SOFTWARE

  # Add hand-maintained machine-specific envy.* policy below this line.

  # BEGIN ENVY MANAGED AGENTS
  # envY owns agent selections, endpoint metadata, and credential references here.

  # --- LLM CREDENTIALS ---
  envy.llm.credentials.deepseek.kind = "api-key";
  envy.llm.credentials.deepseek.secretPath = "llm/deepseek/apikey";
  envy.llm.credentials.hbapi.kind = "api-key";
  envy.llm.credentials.hbapi.secretPath = "llm/hbapi/apikey";
  envy.llm.credentials.mp-new.kind = "api-key";
  envy.llm.credentials.mp-new.secretPath = "llm/mp-new/apikey";
  envy.llm.credentials.mp.kind = "api-key";
  envy.llm.credentials.mp.secretPath = "llm/mp/apikey";
  envy.llm.credentials.stepfun.kind = "api-key";
  envy.llm.credentials.stepfun.secretPath = "llm/steps/apikey";
  envy.llm.credentials.xikai.kind = "api-key";
  envy.llm.credentials.xikai.secretPath = "llm/xikai/apikey";

  # --- LLM ENDPOINTS ---
  envy.llm.endpoints.deepseek-openai.baseUrl = "https://api.deepseek.com";
  envy.llm.endpoints.deepseek-openai.protocol = "openai-compatible";
  envy.llm.endpoints.stepfun-anthropic.baseUrl = "https://mp.z01.normandyfunc.com";
  envy.llm.endpoints.stepfun-anthropic.protocol = "anthropic-compatible";
  envy.llm.endpoints.stepfun-openai.baseUrl = "https://mp.z01.normandyfunc.com/v1";
  envy.llm.endpoints.stepfun-openai.protocol = "openai-compatible";
  envy.llm.endpoints.sub2api-anthropic.baseUrl = "https://49.233.149.230:3389";
  envy.llm.endpoints.sub2api-anthropic.protocol = "anthropic-compatible";
  envy.llm.endpoints.xikai-openai.baseUrl = "https://api.xikai.site/v1";
  envy.llm.endpoints.xikai-openai.protocol = "openai-compatible";

  # --- AGENT PROFILES ---
  envy.agentProfiles.hb-sub2api.api = "anthropic-messages";
  envy.agentProfiles.hb-sub2api.credential = "hbapi";
  envy.agentProfiles.hb-sub2api.endpoint = "sub2api-anthropic";
  envy.agentProfiles.hb-sub2api.model = "claude-opus-5";
  envy.agentProfiles.stepfun-anthropic.api = "anthropic-messages";
  envy.agentProfiles.stepfun-anthropic.credential = "mp-new";
  envy.agentProfiles.stepfun-anthropic.endpoint = "stepfun-anthropic";
  envy.agentProfiles.stepfun-anthropic.model = "claude-opus-5-5-qiye";
  envy.agentProfiles.stepfun-openai-ds.api = "openai-responses";
  envy.agentProfiles.stepfun-openai-ds.credential = "mp-new";
  envy.agentProfiles.stepfun-openai-ds.endpoint = "stepfun-openai";
  envy.agentProfiles.stepfun-openai-ds.model = "deepseek-v4.1-flash-qiye";
  envy.agentProfiles.stepfun-openai-new.api = "openai-responses";
  envy.agentProfiles.stepfun-openai-new.credential = "mp-new";
  envy.agentProfiles.stepfun-openai-new.endpoint = "stepfun-openai";
  envy.agentProfiles.stepfun-openai-new.model = "gpt-6.1-sol-qiye";
  envy.agentProfiles.stepfun-openai.api = "openai-responses";
  envy.agentProfiles.stepfun-openai.credential = "mp";
  envy.agentProfiles.stepfun-openai.endpoint = "stepfun-openai";
  envy.agentProfiles.stepfun-openai.model = "gpt-6-astra";
  envy.agentProfiles.xikai-magpie.api = "openai-responses";
  envy.agentProfiles.xikai-magpie.contextWindow = 272000;
  envy.agentProfiles.xikai-magpie.credential = "xikai";
  envy.agentProfiles.xikai-magpie.endpoint = "xikai-openai";
  envy.agentProfiles.xikai-magpie.maxTokens = 128000;
  envy.agentProfiles.xikai-magpie.model = "gpt-6.1-sol";

  # --- AGENTS ---
  envy.agents.cc.adapter = "claude-code";
  envy.agents.cc.aliases = [ "ccli" ];
  envy.agents.cc.api = "anthropic-messages";
  envy.agents.cc.credential = "mp-new";
  envy.agents.cc.endpoint = "stepfun-anthropic";
  envy.agents.cc.model = "claude-opus-5-5-qiye";
  envy.agents.cc.profile = "stepfun-anthropic";
  envy.agents.codex.adapter = "codex";
  envy.agents.codex.api = "openai-responses";
  envy.agents.codex.credential = "mp-new";
  envy.agents.codex.endpoint = "stepfun-openai";
  envy.agents.codex.model = "gpt-6.1-sol-qiye";
  envy.agents.codex.profile = "stepfun-openai-new";
  envy.agents.pi.adapter = "pi";
  envy.agents.pi.additionalSettings = builtins.fromJSON "{\"compaction\": {\"keepRecentTokens\": 27200, \"reserveTokens\": 40960}, \"defaultTools\": [\"+codemode\"]}";
  envy.agents.pi.api = "anthropic-messages";
  envy.agents.pi.contextWindow = 272000;
  envy.agents.pi.credential = "mp-new";
  envy.agents.pi.endpoint = "stepfun-anthropic";
  envy.agents.pi.maxTokens = 128000;
  envy.agents.pi.model = "claude-opus-5-5-qiye";
  envy.agents.pi.moreThinking = true;
  envy.agents.pi.profile = "stepfun-anthropic";
  # END ENVY MANAGED AGENTS

}
