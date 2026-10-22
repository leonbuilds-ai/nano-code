import { build } from 'vite'
import { cp, mkdir, rm } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'

const here = fileURLToPath(new URL('.', import.meta.url))
await build({ root: here })
const target = resolve(here, '../src/nanocode/static')
await rm(target, { recursive: true, force: true })
await mkdir(target, { recursive: true })
await cp(resolve(here, 'dist'), target, { recursive: true })
