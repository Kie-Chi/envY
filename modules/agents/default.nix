{ academicResearchSkills, academicResearchSkillsCodex, config, emilSkills, gptImage2PptSkills, lib, machinePlatform, pptMaster, vercelAgentSkills, ... }:

{
  imports = [
    ./options.nix
    ./claude.nix
    ./codex.nix
    ./generic.nix
    ./pi.nix
    ./pi-extensions.nix
    ./skills.nix
    ./tools.nix
  ] ++ lib.optionals (machinePlatform == "linux") [ ./linux.nix ];

  # Shared agent defaults. Provider modules define reusable options; this file
  # decides which providers and skills are part of the shared environment.
  agents = {
    claude = {
      # Keep this security-sensitive policy visible in the Nix machine
      # configuration instead of exposing machine-specific agent toggles.
      extraArgs = [ "--dangerously-skip-permissions" ];
      ccliAlias = true;
    };

    skills = {
      catalog = import ./skills/catalog.nix {
        inherit academicResearchSkills academicResearchSkillsCodex emilSkills gptImage2PptSkills pptMaster vercelAgentSkills;
      };
      active = [
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
        "vercel-react-best-practices"
        "vercel-composition-patterns"
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
      baseUrl = lib.mkDefault "${lib.removeSuffix "/" config.envy.llm.steps.url}/v1";
      protocol = lib.mkDefault "openai-compatible";
    };
    stepfun-anthropic = {
      baseUrl = lib.mkDefault config.envy.llm.steps.url;
      protocol = lib.mkDefault "anthropic-compatible";
    };
    deepseek-openai = {
      baseUrl = lib.mkDefault config.envy.llm.deepseek.url;
      protocol = lib.mkDefault "openai-compatible";
    };
  };

  envy.agents.codex = {
    adapter = lib.mkDefault "codex";
    endpoint = lib.mkDefault "stepfun-openai";
    credential = lib.mkDefault "stepfun";
    model = lib.mkDefault config.envy.llm.steps.model;
  };

  envy.agents.cc = {
    adapter = lib.mkDefault "claude-code";
    endpoint = lib.mkDefault "stepfun-anthropic";
    credential = lib.mkDefault "stepfun";
    model = lib.mkDefault config.envy.llm.steps.model;
    # Keep the compatibility alias when no machine block exists. A normal
    # machine-owned value replaces this mkDefault during migration, avoiding
    # list concatenation when the block also declares ccli.
    aliases = lib.mkDefault [ "ccli" ];
  };

  # Pi is the upstream extensible harness.
  envy.agents.pi = {
    adapter = lib.mkDefault "pi";
    endpoint = lib.mkDefault "xikai-openai";
    credential = lib.mkDefault "stepfun";
    model = lib.mkDefault "gpt-6.1-sol";
  };
}
