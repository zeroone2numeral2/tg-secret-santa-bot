# Secret Santa Telegram bot 🤫🎅🏼

Extremely simple and straightforward Telegram bot to organize a [Secret Santa](http://secret-santa.urbanup.com/4845681) in a Telegram group chat.

I was pretty shocked to discover there's not a single basic implementation of a Secret Santa bot on Telegram, so I've decided to spend a couple of hours on this small bot. I've tried to keep it as simple and gimmick-free as possible. The idea is simple: add the bot to a group chat, ask the members to join in, and start the Secret Santas draw once the gang's all in. Easy as that.

### Running an instance of the bot

The is no installation setup/packaging, it's just a Python script you run as you prefer. I'm personally running it using [supervisor](http://supervisord.org) in a virtualenv

1. rename `config.example.toml` to `config.toml`
2. open `config.toml` and set your bot's `[telegram].token`
3. install the requirements via `pip install -r requirements.txt`
4. run the bot with `python main.py`

### The bot

I have an instance running at [@secretsantamatcherbot](https://t.me/secretsantamatcherbot)

### The old Pyrogram implementation

The bot has been rewritten from scratch: the old implementation based on Pyrogram had some drawbacks caused by me not wanting to write a data storage. 
The sourcecode of the old bot can be found [here](https://github.com/zeroone2numeral2/secret-santa-bot)

### Credits

Such a tool was actually suggested by a friend of mine, who was wondering whether there was a way to organize a Secret Santa draw without having all your friends to gather at the same place - which, for various reasons, might not be possible.

If you have a Telegram chat with your friends already set up, a Telegram bot is a pretty handy solution to solve the problem.

After posting the first version on Reddit, a guy pointed out how the bot would work way better by using inline keyboards and asking users to join explicitely. It took me two years to find the time to rewrite it, but here we are! Just in time for Christmas
