FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

ARG VITE_FTA_BACKEND_URL=/api/fta
ARG VITE_KB_BACKEND_URL=/api/kb
ARG VITE_FTA_AI_EDITOR_URL=/api/fta-ai

ENV VITE_FTA_BACKEND_URL=${VITE_FTA_BACKEND_URL} \
    VITE_KB_BACKEND_URL=${VITE_KB_BACKEND_URL} \
    VITE_FTA_AI_EDITOR_URL=${VITE_FTA_AI_EDITOR_URL}

RUN npm run build

FROM nginx:1.27-alpine

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
