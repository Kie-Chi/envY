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
  envy.storage.mode = "report";
  envy.storage.archive.repository = "";

  # --- VSCODE CONFIG ---
  envy.vscode.mode = "remote";

  # Storage rules (report-only; hand-written policy).
  # Report-only policy: planning never moves files; only explicit execution does.
  envy.storage.maintenance = {
    nix = true;
    docker = true;
    go = true;
  };

  envy.storage.protectedOverrides = [
    "/Users/chi/Library/Application Support/LarkShell/aha/users/*/profile_explorer/Service Worker/CacheStorage"
    "/Users/chi/Library/Application Support/LarkShell/iron/users/*/profile_main/Service Worker/CacheStorage"
    "/Users/chi/Library/Application Support/LarkShell/update"
    "/Users/chi/Library/Application Support/LarkShell/sdk_storage/log"
    "/Users/chi/Library/Containers/com.tencent.xinWeChat/Data/Library/Caches"
    "/Users/chi/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/log"
    "/Users/chi/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium/web/profiles"
  ];

  envy.storage.rules = [
    {
      id = "feishu-web-cache-aha";
      path = "/Users/chi/Library/Application Support/LarkShell/aha/users/*/profile_explorer/Service Worker/CacheStorage";
      action = "quarantine";
      targets = "dirs";
      depth = 1;
      recursiveAge = true;
      minAgeDays = 14;
      processes = [ "Feishu" "Lark" ];
    }
    {
      id = "feishu-web-cache-iron";
      path = "/Users/chi/Library/Application Support/LarkShell/iron/users/*/profile_main/Service Worker/CacheStorage";
      action = "quarantine";
      targets = "dirs";
      depth = 1;
      recursiveAge = true;
      minAgeDays = 14;
      processes = [ "Feishu" "Lark" ];
    }
    {
      id = "feishu-update";
      path = "/Users/chi/Library/Application Support/LarkShell/update";
      action = "quarantine";
      targets = "dirs";
      depth = 1;
      recursiveAge = true;
      minAgeDays = 14;
      processes = [ "Feishu" "Lark" ];
    }
    {
      id = "feishu-logs";
      path = "/Users/chi/Library/Application Support/LarkShell/sdk_storage/log";
      action = "quarantine";
      extensions = [ "log" "alaudalog" ];
      minAgeDays = 30;
    }
    {
      id = "codex-sparkle";
      path = "/Users/chi/Library/Caches/com.openai.codex/org.sparkle-project.Sparkle/Installation";
      action = "quarantine";
      targets = "dirs";
      depth = 1;
      recursiveAge = true;
      minAgeDays = 14;
      processes = [ "Codex" "ChatGPT" ];
    }
    {
      id = "vscode-shipit";
      path = "/Users/chi/Library/Caches/com.microsoft.VSCode.ShipIt";
      action = "quarantine";
      targets = "dirs";
      depth = 1;
      recursiveAge = true;
      minAgeDays = 14;
      processes = [ "/Visual Studio Code.app/" ];
    }
    {
      id = "go-build-cache";
      path = "/Users/chi/Library/Caches/go-build";
      action = "quarantine";
      minAgeDays = 30;
    }
    {
      id = "wechat-cache-dirs";
      path = "/Users/chi/Library/Containers/com.tencent.xinWeChat/Data/Library/Caches";
      action = "quarantine";
      targets = "dirs";
      depth = 1;
      recursiveAge = true;
      minAgeDays = 14;
      processes = [ "WeChat" "WeChatAppEx" ];
    }
    {
      id = "wechat-cache-files";
      path = "/Users/chi/Library/Containers/com.tencent.xinWeChat/Data/Library/Caches";
      action = "quarantine";
      minAgeDays = 14;
      minSizeBytes = 1048576;
      processes = [ "WeChat" "WeChatAppEx" ];
    }
    {
      id = "wechat-web-cache";
      path = "/Users/chi/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium/web/profiles/*";
      action = "quarantine";
      targets = "dirs";
      depth = 1;
      names = [ "Cache" ];
      recursiveAge = true;
      minAgeDays = 14;
      processes = [ "WeChat" "WeChatAppEx" ];
    }
    {
      id = "wechat-web-cachestorage";
      path = "/Users/chi/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium/web/profiles/*/Service Worker";
      action = "quarantine";
      targets = "dirs";
      depth = 1;
      names = [ "CacheStorage" ];
      recursiveAge = true;
      minAgeDays = 14;
      processes = [ "WeChat" "WeChatAppEx" ];
    }
    {
      id = "wechat-logs";
      path = "/Users/chi/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/log";
      action = "quarantine";
      extensions = [ "xlog" ];
      minAgeDays = 30;
    }
    {
      id = "old-installers";
      path = "/Users/chi/Downloads";
      action = "quarantine";
      extensions = [ "dmg" "pkg" "iso" ];
      minAgeDays = 30;
    }
  ];

  # END ENVY MANAGED CONFIG

  # BEGIN ENVY MANAGED SOFTWARE
  # `envy setup` and `envy software` own only these machine-local selections.

  envy.darwin.software.homebrew.casks.include = [
    "steam"
  ];

  envy.darwin.software.homebrew.casks.exclude = [
    "trae"
  ];

  envy.darwin.software.homebrew.formulae.include = [
    "hexo"
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
  envy.agents.codex.profile = "deepseek";
  envy.agents.codex.adapter = "codex";
  envy.agents.codex.credential = "xikai";
  envy.agents.codex.endpoint = "xikai-openai";
  envy.agents.codex.model = "gpt-6.1-sol";
  envy.agents.pi.adapter = "pi";
  envy.agents.pi.models = [
    { profile = "xikai-magpie"; model = "gpt-6.1-sol"; }
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
