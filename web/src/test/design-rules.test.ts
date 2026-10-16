import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { join } from 'path';
import { globSync } from 'glob';

describe('Design Rules', () => {
  const root = join(__dirname, '../');

  function getFiles(pattern: string) {
    return globSync(pattern, { cwd: root, ignore: ['generated/**', 'styles/tokens.css', '**/*.test.tsx', '**/*.test.ts'] });
  }

  it('no hex color literals exist outside tokens.css', () => {
    const files = getFiles('**/*.{ts,tsx,css}');
    const hexRegex = /#[0-9a-fA-F]{3,8}\b/;

    for (const file of files) {
      const content = readFileSync(join(root, file), 'utf-8');
      const lines = content.split('\n');
      lines.forEach((line, i) => {
        if (hexRegex.test(line)) {
          throw new Error(`Hex color found in ${file}:${i + 1}\n  ${line.trim()}`);
        }
      });
    }
  });

  it('border-radius is never set to a non-zero value and no box-shadow/gradients', () => {
    const cssFiles = getFiles('**/*.css');

    for (const file of cssFiles) {
      const content = readFileSync(join(root, file), 'utf-8');

      const lines = content.split('\n');
      lines.forEach((line, i) => {
        // match border-radius setting something other than 0
        const brMatch = line.match(/border-radius:\s*([^;]+);/);
        if (brMatch) {
          const val = brMatch[1].trim();
          if (val !== '0') {
            throw new Error(`Non-zero border-radius in ${file}:${i + 1}\n  ${line.trim()}`);
          }
        }

        if (line.includes('box-shadow:')) {
          throw new Error(`box-shadow found in ${file}:${i + 1}\n  ${line.trim()}`);
        }
        if (line.includes('linear-gradient(') || line.includes('radial-gradient(')) {
          throw new Error(`Gradient found in ${file}:${i + 1}\n  ${line.trim()}`);
        }
      });
    }
  });

  it('tokens.css matches docs/design/tokens.css byte for byte', () => {
    const originalTokensPath = join(root, '../../docs/design/tokens.css');
    const localTokensPath = join(root, 'styles/tokens.css');

    const originalTokens = readFileSync(originalTokensPath, 'utf-8');
    const localTokens = readFileSync(localTokensPath, 'utf-8');

    expect(localTokens).toBe(originalTokens);
  });

  it('no link tags in index.html', () => {
    const indexPath = join(root, '../index.html');
    const indexContent = readFileSync(indexPath, 'utf-8');

    if (/<link/i.test(indexContent)) {
      throw new Error(`Found <link> tag in index.html: it should only use web fonts bundled via fontsource.`);
    }
  });
});
