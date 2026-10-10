{ academicResearchSkills, academicResearchSkillsCodex, gptImage2PptSkills, pptMaster }:

{
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
    targets = [ "codex" "claude" "claude-code" "pi" "omp" ];
  };

  gpt-image2-ppt-skills = {
    source = gptImage2PptSkills;
    targets = [ "codex" "claude" "claude-code" "pi" "omp" ];
  };
}
