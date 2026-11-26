# Вышеград РП: как выложить

## 1. GitHub Pages (папка vyshegrad-app)
Новый публичный репозиторий `vyshegrad-app`, залей index.html и admin.html.
Settings -> Pages -> Branch: main, папка / (root).
Адрес: https://ЛОГИН.github.io/vyshegrad-app/index.html

## 2. Railway (папка vyshegrad-bot)
Новый приватный репозиторий `vyshegrad-bot`, залей всё из папки.
Railway -> New Project -> Deploy from GitHub repo.
- Variables: значения из .env.example
- Settings -> Networking -> Generate Domain, этот адрес + /api/me пойдёт в API_URL
- Volumes: создай том, Mount path = /data
- welcome.png (картинка приветствия) положи рядом с bot.py

## 3. Проверка
Адрес бота в браузере должен показать `ok`. Потом /start в Telegram.
