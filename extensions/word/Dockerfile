FROM node:22-alpine

ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH

ARG VITE_FIELDLEDGER_REVIEW_URL=http://127.0.0.1:4173

WORKDIR /app
RUN corepack enable
ENV VITE_FIELDLEDGER_API_URL=/
ENV VITE_FIELDLEDGER_REVIEW_URL=$VITE_FIELDLEDGER_REVIEW_URL

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json ./
COPY packages ./packages
COPY extensions/word ./extensions/word
RUN pnpm install --frozen-lockfile
RUN pnpm --filter @fieldledger/word build

EXPOSE 4174
CMD ["pnpm", "--dir", "extensions/word", "exec", "vite", "preview", "--host", "0.0.0.0", "--port", "4174"]
