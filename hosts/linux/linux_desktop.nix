{ ... }:

{
  imports = [ ../default.nix ];

  # BEGIN ENVY MANAGED CONFIG
  # `envy config` updates only this block. Other machine policy stays intact.

  # --- BASE CONFIG ---
  envy.user.name = "chi";
  envy.user.home = "/home/chi";

  # --- ENV CONFIG ---
  envy.repository.path = "/home/chi/.envy";

  # --- GIT CONFIG ---
  envy.git.name = "Kie-Chi";
  envy.git.email = "137579437@qq.com";

  # --- HABITS CONFIG ---
  envy.habits.terminalScratchpad.gesture = "F12";
  envy.habits.globalLauncher.gesture = "Option+Space";

  # --- LINUX CONFIG ---
  envy.linux.desktop = "all";
  envy.linux.option = "desktop";

  # --- MIRRORS CONFIG ---
  envy.mirrors.mode = "china";

  # --- VSCODE CONFIG ---
  envy.vscode.mode = "remote";
  # END ENVY MANAGED CONFIG

  # BEGIN ENVY MANAGED SOFTWARE
  # `envy setup` and `envy software` own only these machine-local selections.

  envy.software.npm.tools.include = builtins.fromJSON "[{\"id\":\"chatgpt\",\"name\":\"chatgpt\",\"ref\":\"npm:chatgpt\",\"version\":\"5.2.5\"}]";
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
  envy.agents.codex.additionalSettings = {
    model_auto_compact_token_limit = 244800;
    model_context_window = 272000;
  };
  envy.agents.pi.adapter = "pi";
  envy.agents.pi.api = "openai-responses";
  envy.agents.pi.credential = "xikai";
  envy.agents.pi.endpoint = "xikai-openai";
  envy.agents.pi.model = "gpt-6.1-sol";
  envy.agents.pi.contextWindow = 272000;
  envy.agents.pi.maxTokens = 128000;
  envy.agents.pi.additionalSettings = {
    compaction = {
      keepRecentTokens = 27200;
      reserveTokens = 40960;
    };
    defaultTools = [ "+codemode" ];
  };
  # END ENVY MANAGED AGENTS

}
