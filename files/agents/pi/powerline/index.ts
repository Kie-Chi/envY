import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { registerFooter } from "./extensions/footer.ts";

export default function(pi: ExtensionAPI) {
  registerFooter(pi);
}
