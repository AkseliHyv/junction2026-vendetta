import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import fs from 'fs';
import path from 'path';
import {defineConfig, type Plugin} from 'vite';

function serveArduinoAppLabAssets(): Plugin {
  const allowedFiles: Record<string, string> = {
    '/assets/index.html': 'text/html; charset=utf-8',
    '/assets/synth-test.html': 'text/html; charset=utf-8',
    '/assets/synth.js': 'application/javascript; charset=utf-8',
  };
  return {
    name: 'serve-arduino-applab-assets',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const cleanUrl = (req.url || '').split('?')[0];
        const mime = allowedFiles[cleanUrl];
        if (mime) {
          const fullPath = path.resolve(__dirname, '.' + cleanUrl);
          if (fs.existsSync(fullPath)) {
            res.statusCode = 200;
            res.setHeader('Content-Type', mime);
            res.end(fs.readFileSync(fullPath));
            return;
          }
        }
        next();
      });
    },
  };
}

export default defineConfig(() => {
  return {
    plugins: [serveArduinoAppLabAssets(), react(), tailwindcss()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, '.'),
      },
    },
    server: {
      // HMR is disabled in AI Studio via DISABLE_HMR env var.
      // Do not modify—file watching is disabled to prevent flickering during agent edits.
      hmr: process.env.DISABLE_HMR !== 'true',
      // Disable file watching when DISABLE_HMR is true to save CPU during agent edits.
      watch: process.env.DISABLE_HMR === 'true' ? null : {},
    },
  };
});
