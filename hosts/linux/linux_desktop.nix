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

  # BEGIN ENVY MANAGED AGENTS
  # envY owns agent selections, endpoint metadata, and credential references here.

  # --- LLM CREDENTIALS ---
  envy.llm.credentials.deepseek.kind = "api-key";
  envy.llm.credentials.deepseek.secretPath = "llm/deepseek/apikey";
  envy.llm.credentials.stepfun.kind = "api-key";
  envy.llm.credentials.stepfun.secretPath = "llm/steps/apikey";

  # --- LLM ENDPOINTS ---
  envy.llm.endpoints.deepseek-openai.baseUrl = "https://api.deepseek.com";
  envy.llm.endpoints.deepseek-openai.protocol = "openai-compatible";
  envy.llm.endpoints.stepfun-anthropic.baseUrl = "https://models-proxy.stepfun-inc.com";
  envy.llm.endpoints.stepfun-anthropic.protocol = "anthropic-compatible";
  envy.llm.endpoints.stepfun-openai.baseUrl = "https://models-proxy.stepfun-inc.com/v1";
  envy.llm.endpoints.stepfun-openai.protocol = "openai-compatible";

  # --- AGENTS ---
  envy.agents.cc.adapter = "claude-code";
  envy.agents.cc.aliases = [ "ccli" ];
  envy.agents.cc.credential = "stepfun";
  envy.agents.cc.endpoint = "stepfun-anthropic";
  envy.agents.cc.model = "claude-opus-4-6";
  envy.agents.codex.adapter = "codex";
  envy.agents.codex.credential = "stepfun";
  envy.agents.codex.endpoint = "stepfun-openai";
  envy.agents.codex.model = "claude-opus-4-6";
  # END ENVY MANAGED AGENTS

}
