/**
 * После сборки: помечаем каталог CommonJS-сборки как commonjs.
 *
 * В корневом package.json стоит "type": "module", поэтому Node считал бы .js в dist/cjs
 * ESM-файлами. Свой package.json внутри каталога задаёт формат для него — стандартный приём
 * для двойной сборки (import для ESM, require для CommonJS).
 */

import { writeFileSync } from 'node:fs';

writeFileSync(
  new URL('../dist/cjs/package.json', import.meta.url),
  JSON.stringify({ type: 'commonjs' }, null, 2) + '\n',
);

console.log('dist/cjs помечен как commonjs');
