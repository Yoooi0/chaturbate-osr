import asyncio
import json
import logging
import math
import random
import re
import requests
import serial
import string
import sys
import time
import websockets

devicePort = 'COM4'
deviceInterval = 1/60
deviceAxisRange = {
    'L0': [0.3, 0.7],
    'L1': [0.0, 1.0],
    'L2': [0.0, 1.0],
    'R0': [0.0, 1.0],
    'R1': [0.4, 0.6],
    'R2': [0.4, 0.6],
    'V0': [0.0, 1.0],
    'V1': [0.0, 1.0]
}

if len(sys.argv) != 2:
    print('Usage: {} settings-file.json'.format(sys.argv[0]))
    exit()

logging.basicConfig(format='[%(asctime)s][%(levelname)s] %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.info('loading settings: %s', sys.argv[1])
with open(sys.argv[1]) as f:
    settings = json.load(f)

def clamp(v, a, b):
    return max(min(v, b), a)

def clamp01(v):
    return clamp(v, 0, 1)

def lerp(a, b, t):
    return a * (1 - t) + b * t

async def device(loop, queue):
    def getCommand(axis, value):
        range = deviceAxisRange[axis]
        value = lerp(range[0], range[1], value)
        value = clamp(int(value * 1000), 0, 999)
        interval = int(deviceInterval * 1000)
        return '{}{:03d}I{}'.format(axis, value, interval)

    def updateDevice():
        commands = [getCommand(k, v) for k, v in positions.items()]
        logger.debug('devc: %s', ' '.join(commands))
        if device:
            device.write('{}\n'.format(' '.join(commands)).encode())

    def getActions(amount):
        for option in settings['tipmenu']:
            range = option['amount']
            if (len(range) == 1 and range[0] == amount) or (len(range) == 2 and amount >= range[0] and amount <= range[1]):
                return option['actions'], option['duration']

        return None, None

    def getValue(action, t):
        t = (t % action['frequency']) / action['frequency']
        if action['motion'] == 'triangle':
            value = abs(abs(t * 2 - 1.5) - 1)
        elif action['motion'] == 'sine':
            value = -math.sin(t * math.pi * 2) / 2 + 0.5
        elif action['motion'] == 'cosine':
            value = -math.cos(t * math.pi * 2) / 2 + 0.5
        elif action['motion'] == 'bounce':
            x = t * math.pi * 2 - math.pi / 4
            value = -(math.sin(x)**5 + math.cos(x)**5) / 2 + 0.5
        elif action['motion'] == 'sharp':
            x = (t + 0.4195) * math.pi / 2
            s = math.sin(x)**2
            c = math.cos(x)**2
            value = math.sqrt(max(c - s, s - c))

        return clamp01(value)

    async def reset(duration):
        def update(t):
            for k, v in positions.items():
                positions[k] = lerp(positionsCopy[k], defaultPositions[k], t)
            updateDevice()

        logger.info('devc: resetting positions for {}s'.format(duration))
        positionsCopy = positions.copy()
        start = time.perf_counter()
        while((time.perf_counter() - start) <= duration):
            update((time.perf_counter() - start) / duration)
            await asyncio.sleep(deviceInterval)

        update(1)

    async def execute(actions, duration):
        def update(t):
            for action in actions:
                positions[action['axis']] = getValue(action, t)
            updateDevice()

        start = time.perf_counter()
        while((time.perf_counter() - start) <= duration):
            update(time.perf_counter() - start)
            await asyncio.sleep(deviceInterval)

        update(duration)

    try:
        device = serial.Serial(devicePort, 115200)
    except Exception as e:
        device = None
        logger.fatal(e)

    defaultPositions = {
        'L0': 0.5,
        'L1': 0.5,
        'L2': 0.5,
        'R0': 0.5,
        'R1': 0.5,
        'R2': 0.5,
        'V0': 0.0,
        'V1': 0.0
    }
    positions = defaultPositions.copy()

    updateDevice()
    while True:
        amount = await queue.get()
        actions, duration = getActions(amount)
        if not actions:
            logger.warning('devc: could not find actions for %d tip!', amount)
            continue

        s = ', '.join(['[{}, {}, {}]'.format(a['axis'], a['motion'], a['frequency']) for a in actions])
        logger.info('devc: excecuting %d tip: "%s" for %ds', amount, s, duration)

        await execute(actions, duration)
        queue.task_done()

        if queue.empty():
            await reset(1)

async def chaturbate(loop, queue):
    def createMessage(method, data):
        message = json.dumps({
            'method': method,
            'data': data
        })
        message = json.dumps([message])
        logger.debug('sending: %s', message)
        return message

    def createConnectedMessage():
        return createMessage('connect', {
            'user': username,
            'password': password,
            'room': settings['room'],
            'room_password': roomPassword
        })

    def createAuthResponseMessage(result):
        return createMessage('joinRoom', {
            'room': settings['room']
        })

    def pushTip(amount):
        queue.put_nowait(amount)

    response = requests.get('https://chaturbate.com/{}/'.format(settings['room']), headers={'User-Agent': 'Mozilla/5.0'})

    dossier = re.search(r'window\.initialRoomDossier\s?=\s?"(.+?)";', response.text, re.UNICODE | re.MULTILINE).group(1)
    dossier = dossier.encode().decode("unicode-escape")
    dossier = json.loads(dossier)

    if dossier['room_status'] == 'offline':
        logger.info('Room is offline!')
        return

    wschat = dossier['wschat_host']
    wschat = wschat.replace('https://', 'wss://')

    id0 = random.randint(0, 1000)
    id1 = ''.join(random.choice(string.ascii_lowercase) for x in range(8))
    wschat = '{}/{}/{}/websocket'.format(wschat, id0, id1)

    username = dossier['chat_username']
    password = dossier['chat_password']
    roomPassword = dossier['room_pass']

    logger.info('connecting to websocket: %s', wschat)
    start = time.perf_counter()
    async with websockets.connect(wschat) as ws:
        while True:
            message = await ws.recv()
            logger.debug('received: %s', message)

            if not message:
                continue              

            if message[0] == 'o':
                logger.info('sending connect')
                await ws.send(createConnectedMessage())
            elif message[0] == 'h':
                pass
            elif message[0] == 'a':
                data = message[1:]
                data = json.loads(data)
                data = json.loads(data[0])
                
                if data['method'] == 'onAuthResponse':
                    result = int(data['args'][0])
                    logger.info('received onAuthResponse %d', result)

                    if result != 1:
                        logger.critical('Failed to authenticate!')
                        return

                    await ws.send(createAuthResponseMessage(result))
                elif data['method'] == 'onNotify':
                    args = json.loads(data['args'][0])
                    if args['type'] == 'tip_alert':
                        ignored = time.perf_counter() - start < 2
                        logger.info('received tip_alert %s %d %s', args['from_username'], args['amount'], 'ignored' if ignored else '')
                        if not ignored:
                            loop.call_later(settings['delay'], pushTip, args['amount'])
                elif data['method'] == 'onRoomMsg':
                    pass

loop = asyncio.new_event_loop() 
q = asyncio.Queue(loop=loop)  

try:
    loop.create_task(chaturbate(loop, q))
    loop.create_task(device(loop, q))

    loop.run_forever()
finally:
    loop.close()