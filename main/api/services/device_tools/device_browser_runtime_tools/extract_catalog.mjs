// One-off helper: regenerate catalog.json from extension definitions.ts
import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'
import vm from 'vm'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

// Support both old monorepo and new multi-repo workspace layouts.
// From server/ after split the device is usually at ../device relative to workspace.
const candidates = [
  path.resolve(__dirname, '../../../../../device/extension/src/lib/tools/definitions.ts'), // old monorepo
  path.resolve(__dirname, '../../../../device/extension/src/lib/tools/definitions.ts'),     // server at workspace root
  path.resolve(__dirname, '../../../../../device/extension/src/lib/tools/definitions.ts'),    // safety
  path.resolve(process.cwd(), 'device/extension/src/lib/tools/definitions.ts'),
  path.resolve(process.cwd(), '../device/extension/src/lib/tools/definitions.ts'),
  path.resolve(process.cwd(), '../../device/extension/src/lib/tools/definitions.ts'),
]

let defsPath = null
for (const c of candidates) {
  if (fs.existsSync(c)) { defsPath = c; break }
}
if (!defsPath) {
  throw new Error('Could not locate device/extension/src/lib/tools/definitions.ts. Run from workspace root or set correct relative path.')
}
let src = fs.readFileSync(defsPath, 'utf8')
src = src.replace(/^import.*$/gm, '')
src = src.replace(/export /g, '')
src = src.replace(/: AIToolDef\[\]/g, '')
const capIdx = src.indexOf('const BROWSER_CAPABILITIES')
if (capIdx > 0) src = src.slice(0, capIdx)

const out = vm.runInNewContext(
  src + '; JSON.stringify(BROWSER_TOOLS.map(t => ({ name: t.name, description: t.description, input_schema: t.input_schema })), null, 2)',
  {},
)
const catalog = JSON.parse(out)
fs.writeFileSync(path.join(__dirname, 'catalog.json'), JSON.stringify(catalog, null, 2), 'utf8')
console.log(`wrote ${catalog.length} tools to catalog.json`)
