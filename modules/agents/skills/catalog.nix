{ academicResearchSkills, academicResearchSkillsCodex, emilSkills, gptImage2PptSkills, pptMaster, vercelAgentSkills }:

{
  read = {
    source = ./read;
    targets = [ "pi" ];
  };

  browser = {
    source = ./browser;
    targets = [ "pi" ];
  };

  # Codex uses the upstream native adapter as one router skill. The internal
  # workflows deliberately use WORKFLOW.md so Codex discovers only this entry.
  academic-research-suite = {
    source = academicResearchSkillsCodex + "/skills/academic-research-suite";
    targets = [ "codex" ];
  };

  # Claude Code's upstream distribution exposes four coordinated skills.
  academic-paper = {
    source = academicResearchSkills + "/academic-paper";
    targets = [ "claude" ];
  };

  academic-paper-reviewer = {
    source = academicResearchSkills + "/academic-paper-reviewer";
    targets = [ "claude" ];
  };

  academic-pipeline = {
    source = academicResearchSkills + "/academic-pipeline";
    targets = [ "claude" ];
  };

  deep-research = {
    source = academicResearchSkills + "/deep-research";
    targets = [ "claude" ];
  };

  ppt-master = {
    source = pptMaster + "/ppt-master";
    targets = [ "codex" "claude" "claude-code" "pi" ];
  };

  gpt-image2-ppt-skills = {
    source = gptImage2PptSkills;
    targets = [ "codex" "claude" "claude-code" "pi" ];
  };

  # Keep the complete upstream directories: animate and break-ui load their
  # companion recipe/catalog documents only when the task needs them.
  emil-design-eng = {
    source = emilSkills + "/skills/emil-design-eng";
    targets = [ "codex" "claude" "claude-code" "pi" ];
  };

  animate = {
    source = emilSkills + "/skills/animate";
    targets = [ "codex" "claude" "claude-code" "pi" ];
  };

  # animate delegates component selection here. Preserve upstream's explicit
  # invocation metadata while making the companion workflow available.
  pick-ui-library = {
    source = emilSkills + "/skills/pick-ui-library";
    targets = [ "codex" "claude" "claude-code" "pi" ];
  };

  animation-performance = {
    source = emilSkills + "/skills/animation-performance";
    targets = [ "codex" "claude" "claude-code" "pi" ];
  };

  break-ui = {
    source = emilSkills + "/skills/break-ui";
    targets = [ "codex" "claude" "claude-code" "pi" ];
  };

  # Registry IDs match SKILL.md names rather than upstream directory names.
  vercel-react-best-practices = {
    source = vercelAgentSkills + "/skills/react-best-practices";
    targets = [ "codex" "claude" "claude-code" "pi" ];
  };

  vercel-composition-patterns = {
    source = vercelAgentSkills + "/skills/composition-patterns";
    targets = [ "codex" "claude" "claude-code" "pi" ];
  };
}
