{ academicResearchSkills, academicResearchSkillsCodex, antiDefensiveWriting, emilSkills, gptImage2PptSkills, lib, pptMaster, promptMaster, vercelAgentSkills, wazaSource, ... }:

{
  home.file = {
    ".pi/agent/skills/read".recursive = true;
    ".pi/agent/skills/read/references".source = wazaSource + "/skills/read/references";
    ".pi/agent/skills/read/LICENSE".source = wazaSource + "/LICENSE";
  };

  # Shared agent defaults. Provider modules define reusable options; this file
  # decides which providers and skills are part of the shared environment.
  agents = {
    skills = {
      catalog = import ./skills/catalog.nix {
        inherit academicResearchSkills academicResearchSkillsCodex antiDefensiveWriting emilSkills gptImage2PptSkills pptMaster promptMaster vercelAgentSkills wazaSource;
      };
      active = [
        "anti-defensive-writing"
        "academic-research-suite"
        "academic-paper"
        "academic-paper-reviewer"
        "academic-pipeline"
        "deep-research"
        "ppt-master"
        "gpt-image2-ppt-skills"
        "emil-design-eng"
        "animate"
        "pick-ui-library"
        "animation-performance"
        "break-ui"
        "review-animations"
        "improve-animations"
        "find-animation-opportunities"
        "vercel-react-best-practices"
        "vercel-composition-patterns"
        "web-design-guidelines"
        "prompt-master"
        "health"
        "hunt"
        "check"
        "read"
        "browser"
      ];
    };
  };

  # The defaults preserve the existing Codex/Claude setup while making the
  # machine-facing endpoint and credential relationship explicit. Additional
  # agents such as dsh and stepgo are ordinary attrs under envy.agents.
  envy.llm.credentials = {
    stepfun = {
      kind = lib.mkDefault "api-key";
      secretPath = lib.mkDefault "llm/steps/apikey";
    };
    deepseek = {
      kind = lib.mkDefault "api-key";
      secretPath = lib.mkDefault "llm/deepseek/apikey";
    };
  };

  envy.llm.endpoints = {
    stepfun-openai = {
      baseUrl = lib.mkDefault "https://stepfun.com/v1";
      protocol = lib.mkDefault "openai-compatible";
    };
    stepfun-anthropic = {
      baseUrl = lib.mkDefault "https://stepfun.com";
      protocol = lib.mkDefault "anthropic-compatible";
    };
    deepseek-openai = {
      baseUrl = lib.mkDefault "https://api.deepseek.com";
      protocol = lib.mkDefault "openai-compatible";
    };
  };

  envy.agents.codex = {
    adapter = lib.mkDefault "codex";
    endpoint = lib.mkDefault "stepfun-openai";
    credential = lib.mkDefault "stepfun";
    model = lib.mkDefault "step-3.7-flash";
  };

  envy.agents.cc = {
    adapter = lib.mkDefault "claude-code";
    endpoint = lib.mkDefault "stepfun-anthropic";
    credential = lib.mkDefault "stepfun";
    model = lib.mkDefault "step-3.7-flash";
    # Keep this security-sensitive policy visible in the shared Nix
    # configuration instead of exposing a machine-specific agent toggle.
    # A plain definition concatenates with machine-owned args.
    args = [ "--dangerously-skip-permissions" ];
    # Keep the ccli alias when no machine block exists. A machine-owned value
    # replaces this mkDefault, avoiding list concatenation when the block also
    # declares ccli.
    aliases = lib.mkDefault [ "ccli" ];
    # Claude Code installs enabled marketplace plugins and owns their cache.
    additionalSettings.enabledPlugins = {
      "skill-creator@claude-plugins-official" = lib.mkDefault true;
      "code-review@claude-plugins-official" = lib.mkDefault true;
    };
  };

  # Pi is the upstream extensible harness.
  envy.agents.pi = {
    adapter = lib.mkDefault "pi";
    # Route active tool calls through codemode so the Pi prompt's orchestration
    # policy is enforced by the loadout, while pure conversational replies stay
    # tool-free.
    additionalSettings.codemode.mode = lib.mkDefault "only";
    additionalSettings.defaultTools = lib.mkDefault [ "+codemode" ];
    # xikai is a Darwin machine-owned endpoint; the portable default must use
    # the shared StepFun endpoint so Linux targets remain configured.
    endpoint = lib.mkDefault "stepfun-openai";
    credential = lib.mkDefault "stepfun";
    model = lib.mkDefault "gpt-6.1-sol";
  };
}
