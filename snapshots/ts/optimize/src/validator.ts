// Lazy JSON Schema validator for the MCP SDK's `jsonSchemaValidator` option.
// The SDK's Server and Client constructors build an Ajv instance (plus formats) eagerly, which costs startup time
// and memory. Nothing in tinycoder needs it unless a tool declares an outputSchema (none do) or a server
// elicits input (never). This wrapper builds the SDK's own AjvJsonSchemaValidator only on first use, so
// validation behaviour is unchanged.
import type { jsonSchemaValidator, JsonSchemaType, JsonSchemaValidator } from "@modelcontextprotocol/sdk/validation";

export class LazyValidator implements jsonSchemaValidator {
  private real: jsonSchemaValidator | undefined;
  getValidator<T>(schema: JsonSchemaType): JsonSchemaValidator<T> {
    if (!this.real) {
      const { AjvJsonSchemaValidator } = require("@modelcontextprotocol/sdk/validation/ajv") as typeof import("@modelcontextprotocol/sdk/validation/ajv");
      this.real = new AjvJsonSchemaValidator();
    }
    return this.real.getValidator<T>(schema);
  }
}
