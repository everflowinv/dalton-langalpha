import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { createPluginDefinition } from "./src/plugin-definition.mjs";

export default definePluginEntry(createPluginDefinition());
export { createPluginDefinition };
