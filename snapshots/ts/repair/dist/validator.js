"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.LazyValidator = void 0;
class LazyValidator {
    real;
    getValidator(schema) {
        if (!this.real) {
            const { AjvJsonSchemaValidator } = require("@modelcontextprotocol/sdk/validation/ajv");
            this.real = new AjvJsonSchemaValidator();
        }
        return this.real.getValidator(schema);
    }
}
exports.LazyValidator = LazyValidator;
