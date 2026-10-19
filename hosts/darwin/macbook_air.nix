{ ... }:

{
  imports = [ ../default.nix ];

  # `envy config refine` writes non-sensitive machine values here.
  # Add machine-specific envy.* overrides here.

  # BEGIN ENVY MANAGED CONFIG
  # `envy config` updates only this block. Other machine policy stays intact.

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

  # --- VSCODE CONFIG ---
  envy.vscode.mode = "remote";
  # END ENVY MANAGED CONFIG

  # BEGIN ENVY MANAGED SOFTWARE
  # `envy setup` and `envy software` own only these machine-local selections.

  envy.darwin.software.homebrew.casks.include = [
    "steam"
  ];

  envy.darwin.software.homebrew.casks.exclude = [
    "trae"
  ];

  envy.software.pypi.tools.exclude = [
    "headroom"
  ];
  # END ENVY MANAGED SOFTWARE

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

  # --- AGENT PROFILES ---
  envy.agentProfiles.xikai-magpie.api = "openai-responses";
  envy.agentProfiles.xikai-magpie.contextWindow = 272000;
  envy.agentProfiles.xikai-magpie.credential = "xikai";
  envy.agentProfiles.xikai-magpie.endpoint = "xikai-openai";
  envy.agentProfiles.xikai-magpie.maxTokens = 128000;
  envy.agentProfiles.xikai-magpie.model = "gpt-6.1-sol";

  envy.agentProfiles.hb-sub2api.api = "anthropic-messages";
  envy.agentProfiles.hb-sub2api.credential = "hbapi";
  envy.agentProfiles.hb-sub2api.endpoint = "sub2api-anthropic";
  envy.agentProfiles.hb-sub2api.model = "claude-opus-5";

  envy.agentProfiles.stepfun-openai.api = "openai-completions";
  envy.agentProfiles.stepfun-openai.credential = "stepfun";
  envy.agentProfiles.stepfun-openai.endpoint = "stepfun-openai";
  envy.agentProfiles.stepfun-openai.model = "step-3.7-flash";

  envy.agentProfiles.stepfun-anthropic.api = "anthropic-messages";
  envy.agentProfiles.stepfun-anthropic.credential = "stepfun";
  envy.agentProfiles.stepfun-anthropic.endpoint = "stepfun-anthropic";
  envy.agentProfiles.stepfun-anthropic.model = "step-3.7-flash";

  envy.agentProfiles.deepseek.api = "openai-completions";
  envy.agentProfiles.deepseek.credential = "deepseek";
  envy.agentProfiles.deepseek.endpoint = "deepseek-openai";
  envy.agentProfiles.deepseek.model = "deepseek-v4-pro";

  # --- AGENTS ---
  envy.agents.cc.adapter = "claude-code";
  envy.agents.cc.aliases = [ "ccli" ];
  envy.agents.cc.credential = "hbapi";
  envy.agents.cc.endpoint = "sub2api-anthropic";
  envy.agents.cc.model = "claude-opus-5";
  envy.agents.codex.adapter = "codex";
  envy.agents.codex.credential = "xikai";
  envy.agents.codex.endpoint = "xikai-openai";
  envy.agents.codex.model = "gpt-6.1-sol";
  envy.agents.pi.adapter = "pi";
  envy.agents.pi.additionalSettings = builtins.fromJSON "{\"compaction\": {\"keepRecentTokens\": 27200, \"reserveTokens\": 27200}, \"defaultTools\": [\"+codemode\"]}";
  envy.agents.pi.api = "openai-responses";
  envy.agents.pi.contextWindow = 272000;
  envy.agents.pi.credential = "xikai";
  envy.agents.pi.endpoint = "xikai-openai";
  envy.agents.pi.maxTokens = 128000;
  envy.agents.pi.model = "gpt-6.1-sol";
  envy.agents.pi.moreThinking = true;
  # END ENVY MANAGED AGENTS

}
