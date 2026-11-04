{ config, pkgs, lib, sys, ... }:

let
  translateForZoteroVersion = "2.4.5";
  translateForZotero = pkgs.fetchurl {
    url = "https://github.com/windingwind/zotero-pdf-translate/releases/download/v${translateForZoteroVersion}/translate-for-zotero.xpi";
    hash = "sha256-pELlTT/l9aEo7ev86eu1TKo7Zkviom7YALTI6mqAi3A=";
  };

  zoteroRoot = "${config.home.homeDirectory}/Library/Application Support/Zotero";
  # The translation services use fixed provider endpoints and models; only
  # their keys come from the logical stepfun and deepseek credentials.
  claudeEndpoint = "https://stepfun.com/v1/messages";
  claudeModel = "step-3.7-flash";
  deepseekChatEndpoint = "https://api.deepseek.com/v1/chat/completions";
  deepseekModel = "deepseek-v4-pro";
  deepseekCustomParams = builtins.toJSON { max_tokens = 4096; };
  managedMarker = "// Managed by nix-darwin/home-manager: modules/desktops/darwin/zotero.nix";
  legacyManagedMarker = "// Managed by nix-darwin/home-manager: modules/desktops/zotero.nix";
  translationPrompt = ''
    As an academic expert, translate the following text from ''${langFrom} to ''${langTo}. Preserve technical terminology, equations, citation markers, and paragraph structure. Use the paper title and abstract as context when they are available. Return only the translation without commentary: ''${sourceText}
  '';

  defaultProfilesIni = pkgs.writeText "zotero-profiles.ini" ''
    [Profile0]
    Name=default
    IsRelative=1
    Path=Profiles/nix.default
    Default=1

    [General]
    StartWithLastProfile=1
    Version=2
  '';

  configureZotero = pkgs.writeShellScript "configure-zotero" ''
      set -euo pipefail

      log_info() { printf '[INFO][configureZotero] %s\n' "$*" >&2; }
      log_warn() { printf '[WARN][configureZotero] %s\n' "$*" >&2; }

      ZOTERO_ROOT=${lib.escapeShellArg zoteroRoot}
      PROFILES_INI="$ZOTERO_ROOT/profiles.ini"
      STEPFUN_KEY_FILE=${lib.escapeShellArg config.sops.secrets.llm-credential-stepfun.path}
      DEEPSEEK_KEY_FILE=${lib.escapeShellArg config.sops.secrets.llm-credential-deepseek.path}

      ${sys.cmds.mkdir} -p "$ZOTERO_ROOT"
      if [ ! -f "$PROFILES_INI" ]; then
        ${sys.cmds.mkdir} -p "$ZOTERO_ROOT/Profiles/nix.default"
        ${sys.cmds.install} -m 0600 ${defaultProfilesIni} "$PROFILES_INI"
        log_info "Created the default Zotero profile"
      fi

      PROFILE_PATH="$(${sys.cmds.awk} -F= '
        /^\[Profile/ { in_profile=1; path=""; is_default="0"; next }
        /^\[/ {
          if (in_profile && is_default == "1" && path != "") {
            print path
            found=1
            exit
          }
          in_profile=0
        }
        in_profile && $1 == "Path" { path=substr($0, index($0, "=") + 1) }
        in_profile && $1 == "Default" { is_default=$2 }
        END {
          if (!found && in_profile && is_default == "1" && path != "") print path
        }
      ' "$PROFILES_INI")"

      if [ -z "$PROFILE_PATH" ]; then
        PROFILE_PATH="$(${sys.cmds.awk} -F= '$1 == "Path" { print substr($0, index($0, "=") + 1); exit }' "$PROFILES_INI")"
      fi

      if [ -z "$PROFILE_PATH" ]; then
        log_warn "No Zotero profile was found; open Zotero once and re-run envy apply"
      else
        case "$PROFILE_PATH" in
          /*) PROFILE_DIR="$PROFILE_PATH" ;;
          *) PROFILE_DIR="$ZOTERO_ROOT/$PROFILE_PATH" ;;
        esac

        EXTENSIONS_DIR="$PROFILE_DIR/extensions"
        PLUGIN_TARGET="$EXTENSIONS_DIR/zoteropdftranslate@euclpts.com.xpi"
        USER_JS="$PROFILE_DIR/user.js"

        ${sys.cmds.mkdir} -p "$EXTENSIONS_DIR"
        if [ ! -f "$PLUGIN_TARGET" ] || ! ${sys.cmds.cmp} -s ${translateForZotero} "$PLUGIN_TARGET"; then
          ${sys.cmds.install} -m 0644 ${translateForZotero} "$PLUGIN_TARGET"
          log_info "Installed Translate for Zotero ${translateForZoteroVersion}"
        fi

        if [ ! -s "$DEEPSEEK_KEY_FILE" ] && [ ! -s "$STEPFUN_KEY_FILE" ]; then
          log_warn "No configured LLM API key is available; Zotero translation preferences were not rendered"
        else
          if [ -s "$DEEPSEEK_KEY_FILE" ]; then
            TRANSLATE_SOURCE="customgpt1"
            if [ -s "$STEPFUN_KEY_FILE" ]; then
              SECRET_OBJECT="$(${pkgs.jq}/bin/jq -cn \
                --rawfile deepseek "$DEEPSEEK_KEY_FILE" \
                --rawfile stepfun "$STEPFUN_KEY_FILE" \
                '{
                  customgpt1: ($deepseek | gsub("[\\r\\n]+$"; "")),
                  claude: ($stepfun | gsub("[\\r\\n]+$"; ""))
                }')"
            else
              SECRET_OBJECT="$(${pkgs.jq}/bin/jq -cn --rawfile deepseek "$DEEPSEEK_KEY_FILE" \
                '{customgpt1: ($deepseek | gsub("[\\r\\n]+$"; ""))}')"
            fi
          else
            TRANSLATE_SOURCE="claude"
            SECRET_OBJECT="$(${pkgs.jq}/bin/jq -cn --rawfile stepfun "$STEPFUN_KEY_FILE" \
              '{claude: ($stepfun | gsub("[\\r\\n]+$"; ""))}')"
          fi

          TRANSLATE_SOURCE_LITERAL="$(printf '%s' "$TRANSLATE_SOURCE" | ${pkgs.jq}/bin/jq -Rs .)"
          SECRET_LITERAL="$(printf '%s' "$SECRET_OBJECT" | ${pkgs.jq}/bin/jq -Rs .)"
          USER_JS_TMP="$(${sys.cmds.mktemp})"

          umask 077
          {
            printf '%s\n' ${lib.escapeShellArg managedMarker}
            printf 'user_pref("extensions.zotero.ZoteroPDFTranslate.translateSource", %s);\n' "$TRANSLATE_SOURCE_LITERAL"
            printf '%s\n' 'user_pref("extensions.zotero.ZoteroPDFTranslate.sourceLanguage", "en-US");'
            printf '%s\n' 'user_pref("extensions.zotero.ZoteroPDFTranslate.targetLanguage", "zh-CN");'
            printf '%s\n' 'user_pref("extensions.zotero.ZoteroPDFTranslate.enableAuto", false);'
            printf '%s\n' 'user_pref("extensions.zotero.ZoteroPDFTranslate.attachPaperContext", true);'
            printf '%s\n' 'user_pref("extensions.zotero.ZoteroPDFTranslate.hideUnconfiguredServices", true);'
            printf '%s\n' 'user_pref("extensions.zotero.ZoteroPDFTranslate.customGPT1.endPoint", ${builtins.toJSON deepseekChatEndpoint});'
            printf '%s\n' 'user_pref("extensions.zotero.ZoteroPDFTranslate.customGPT1.model", ${builtins.toJSON deepseekModel});'
            printf '%s\n' 'user_pref("extensions.zotero.ZoteroPDFTranslate.customGPT1.temperature", "0.3");'
            printf '%s\n' 'user_pref("extensions.zotero.ZoteroPDFTranslate.customGPT1.prompt", ${builtins.toJSON translationPrompt});'
            printf '%s\n' 'user_pref("extensions.zotero.ZoteroPDFTranslate.customGPT1.stream", true);'
            printf '%s\n' 'user_pref("extensions.zotero.ZoteroPDFTranslate.customGPT1.customParams", ${builtins.toJSON deepseekCustomParams});'
            printf '%s\n' 'user_pref("extensions.zotero.ZoteroPDFTranslate.claude.endPoint", ${builtins.toJSON claudeEndpoint});'
            printf '%s\n' 'user_pref("extensions.zotero.ZoteroPDFTranslate.claude.model", ${builtins.toJSON claudeModel});'
            printf '%s\n' 'user_pref("extensions.zotero.ZoteroPDFTranslate.claude.temperature", "0.3");'
            printf '%s\n' 'user_pref("extensions.zotero.ZoteroPDFTranslate.claude.maxTokens", "4000");'
            printf '%s\n' 'user_pref("extensions.zotero.ZoteroPDFTranslate.claude.stream", true);'
            printf 'user_pref("extensions.zotero.ZoteroPDFTranslate.secretObj", %s);\n' "$SECRET_LITERAL"
          } > "$USER_JS_TMP"

          if [ -f "$USER_JS" ] \
            && ! ${sys.cmds.grep} -Fqx ${lib.escapeShellArg managedMarker} "$USER_JS" \
            && ! ${sys.cmds.grep} -Fqx ${lib.escapeShellArg legacyManagedMarker} "$USER_JS"; then
            if [ ! -e "$USER_JS.pre-nix" ]; then
              ${sys.cmds.cp} -p "$USER_JS" "$USER_JS.pre-nix"
              log_info "Backed up the existing Zotero user.js"
            fi
          fi

          if [ ! -f "$USER_JS" ] || ! ${sys.cmds.cmp} -s "$USER_JS_TMP" "$USER_JS"; then
            ${sys.cmds.install} -m 0600 "$USER_JS_TMP" "$USER_JS"
            log_info "Rendered Zotero translation preferences from the existing LLM configuration"
          fi
          ${sys.cmds.rm} -f "$USER_JS_TMP"

          if ${sys.cmds.pgrep} -x Zotero >/dev/null 2>&1 || ${sys.cmds.pgrep} -x zotero >/dev/null 2>&1; then
            log_warn "Restart Zotero to load the managed translation preferences"
          fi
        fi
      fi
  '';
in
{
  home.activation.configureZotero = lib.mkIf (
    !(builtins.elem "zotero" config.envy.darwin.software.homebrew.casks.exclude)
  ) (sys.task.activation {
    name = "configureZotero";
    script = "${configureZotero}";
  });
}
