import { applyValidationErrors, computeDirty, hydrateSettings, serializeSettings } from './settings-model.js';

export class SettingsPageState {
  constructor() { this.reset(); }
  reset(payload = {}) { Object.assign(this, hydrateSettings(payload)); }
  update(mutator) { mutator(this.draft); return this.dirty; }
  get dirty() { return computeDirty(this.draft, this.persisted); }
  get payload() { return serializeSettings(this.draft); }
  saved(persisted, pendingFields = []) { this.persisted = hydrateSettings({ persisted, active: this.active }).persisted; this.draft = structuredClone(this.persisted); this.pendingFields = pendingFields; this.fieldErrors = {}; }
  failed(fieldErrors) { this.fieldErrors = applyValidationErrors(fieldErrors); }
}
