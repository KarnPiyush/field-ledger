FROM node:22-alpine

ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH

WORKDIR /app
RUN corepack enable
ENV VITE_API_BASE_URL=/

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json ./
COPY packages ./packages
COPY apps/mobile ./apps/mobile
RUN pnpm install --frozen-lockfile
RUN pnpm --filter @fieldledger/mobile build

EXPOSE 4173
CMD ["pnpm", "--dir", "apps/mobile", "exec", "vite", "preview", "--host", "0.0.0.0", "--port", "4173"]
