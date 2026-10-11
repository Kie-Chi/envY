{ ... }:

{
  imports = [ ../default.nix ];

  # BEGIN ENVY MANAGED CONFIG
  # `envy config` updates only this block. Other machine policy stays intact.

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

  # --- AGENTS ---
  envy.agents.cc.adapter = "claude-code";
  envy.agents.cc.aliases = [ "ccli" ];
  envy.agents.cc.credential = "hbapi";
  envy.agents.cc.endpoint = "sub2api-anthropic";
  envy.agents.cc.model = "opus-5";
  envy.agents.codex.adapter = "codex";
  envy.agents.codex.credential = "xikai";
  envy.agents.codex.endpoint = "xikai-openai";
  envy.agents.codex.model = "gpt-6.1-sol";
  envy.agents.omp.adapter = "oh-my-pi";
  envy.agents.omp.credential = "xikai";
  envy.agents.omp.endpoint = "xikai-openai";
  envy.agents.omp.model = "gpt-6.1-sol";
  envy.agents.pi.adapter = "pi";
  envy.agents.pi.credential = "xikai";
  envy.agents.pi.endpoint = "xikai-openai";
  envy.agents.pi.model = "gpt-6.1-sol";
  # END ENVY MANAGED AGENTS

}
