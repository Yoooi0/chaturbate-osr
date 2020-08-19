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

deviceSettings = {
    'port': 'COM4',
    'interval': 1 / 60,
    'range': {
        'L0': [0.0, 1.0],
        'L1': [0.0, 1.0],
        'L2': [0.0, 1.0],
        'R0': [0.0, 1.0],
        'R1': [0.0, 1.0],
        'R2': [0.0, 1.0],
        'V0': [0.0, 1.0],
        'V1': [0.0, 1.0]
    }
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
    settings['device'] = deviceSettings

def clamp(v, a, b):
    return max(min(v, b), a)

def clamp01(v):
    return clamp(v, 0, 1)

def lerp(a, b, t):
    return a * (1 - t) + b * t

class AbstractDevice():
    def __init__(self, loop, queue):
        self.loop = loop
        self.queue = queue

        try:
            self.device = serial.Serial(settings['device']['port'], 115200)
        except Exception as e:
            self.device = None
            logger.fatal(e)

        self.defaultPositions = {
            'L0': 0.5,
            'L1': 0.5,
            'L2': 0.5,
            'R0': 0.5,
            'R1': 0.5,
            'R2': 0.5,
            'V0': 0.0,
            'V1': 0.0
        }
        self.positions = self.defaultPositions.copy()
        self.updateDevice()

    async def run(self):
        pass

    def getCommand(self, axis, value):
        range = settings['device']['range'][axis]
        value = lerp(range[0], range[1], value)
        value = clamp(int(value * 1000), 0, 999)
        interval = int(settings['device']['interval'] * 1000)
        return '{}{:03d}I{}'.format(axis, value, interval)

    def updateDevice(self):
        commands = [self.getCommand(k, v) for k, v in self.positions.items()]
        logger.debug('devc: %s', ' '.join(commands))
        if self.device:
            self.device.write('{}\n'.format(' '.join(commands)).encode())

class TipMenuDevice(AbstractDevice):
    async def run(self):
        while True:
            amount = await self.queue.get()
            await self.process(amount)
            self.queue.task_done()

            if self.queue.empty():
                await self.reset(1)

    async def process(self, amount):
        actions, duration = self.getActions(amount)
        if not actions:
            logger.warning('devc: could not find actions for %d tip!', amount)
            return

        s = ', '.join(['[{}, {}, {}]'.format(a['axis'], a['motion'], a['frequency']) for a in actions])
        logger.info('devc: excecuting %d tip: "%s" for %ds', amount, s, duration)

        await self.execute(actions, duration)

    async def execute(self, actions, duration):
        def update(t):
            for action in actions:
                if t < action.get('delay', 0):
                    continue

                actionT = ((t + action.get('offset', 0)) % action['frequency']) / action['frequency']
                self.positions[action['axis']] = self.getValue(action, actionT)
            
            resetT = clamp01(t / min(duration, 1))
            for axis in idleAxes:
                self.positions[axis] = lerp(positionsCopy[axis], self.defaultPositions[axis], resetT)

            self.updateDevice()

        idleAxes = [axis for axis in self.positions.keys() if not axis in [action['axis'] for action in actions]]
        positionsCopy = self.positions.copy()

        start = time.perf_counter()
        while((time.perf_counter() - start) <= duration):
            update(time.perf_counter() - start)
            await asyncio.sleep(settings['device']['interval'])

        update(duration)

    async def reset(self, duration):
        def update(t):
            for k, v in self.positions.items():
                self.positions[k] = lerp(positionsCopy[k], self.defaultPositions[k], t)
            self.updateDevice()

        logger.info('devc: resetting positions for {}s'.format(duration))
        positionsCopy = self.positions.copy()

        start = time.perf_counter()
        while((time.perf_counter() - start) <= duration):
            update((time.perf_counter() - start) / duration)
            await asyncio.sleep(settings['device']['interval'])

        update(1)

    def getActions(self, amount):
        for option in settings['tipmenu']:
            range = option['amount']
            if (len(range) == 1 and range[0] == amount) or (len(range) == 2 and amount >= range[0] and amount <= range[1]):
                return option['actions'], option['duration']

        return None, None
    
    def getValue(self, action, t):
        value = self.defaultPositions[action['axis']]

        if action['motion'] == 'triangle':
            value = abs(abs(t * 2 - 1.5) - 1)
        elif action['motion'] == 'sine':
            value = -math.sin(t * math.pi * 2) / 2 + 0.5
        elif action['motion'] == 'bounce':
            x = t * math.pi * 2 - math.pi / 4
            value = -(math.sin(x)**5 + math.cos(x)**5) / 2 + 0.5
        elif action['motion'] == 'sharp':
            x = (t + 0.4195) * math.pi / 2
            s = math.sin(x)**2
            c = math.cos(x)**2
            value = math.sqrt(max(c - s, s - c))

        return clamp01(value)

class ExcitementDevice(AbstractDevice):
    def __init__(self, loop, queue):
        AbstractDevice.__init__(self, loop, queue)

        self.excitiment = 0.0
        self.lastTipTime = time.perf_counter()
        self.dt = 0.01
        self.tick = 0

    async def run(self):
        while True:
            if not self.queue.empty():
                amount = await self.queue.get()
                self.process(amount)
                self.queue.task_done()

            await self.execute()

    def process(self, amount):
        self.lastTipTime = time.perf_counter()
        self.excitiment = clamp01(self.excitiment + amount / 500)

        logger.info('devc: excecuting %d tip', amount)
    
    async def execute(self):
        alpha = lerp(1.5, 0.5, (time.perf_counter() - self.lastTipTime) / 12)
        decay = math.pow(self.excitiment, math.pow(math.e, alpha)) / 8

        self.excitiment = clamp01(self.excitiment - decay * settings['device']['interval'])

        targetScale = lerp(0.005, 0.5, self.excitiment)
        spread = lerp(0.25, 1, self.excitiment)
        self.dt = lerp(self.dt, targetScale, 0.05)
        self.tick += self.dt
        
        self.positions['L0'] = lerp(0.5 - spread / 2, 0.5 + spread / 2, (math.sin(self.tick) + 1) / 2) 
        self.updateDevice()

        logger.info('devc: excitiment: %f decay: %f dt: %f spread: %f L0: %f', self.excitiment, decay, self.dt, spread, self.positions['L0'])
        await asyncio.sleep(settings['device']['interval'])


class Chaturbate():
    def __init__(self, loop, queue):
        self.loop = loop
        self.queue = queue

        response = requests.get('https://chaturbate.com/{}/'.format(settings['room']), headers={'User-Agent': 'Mozilla/5.0'})

        dossier = re.search(r'window\.initialRoomDossier\s?=\s?"(.+?)";', response.text, re.UNICODE | re.MULTILINE).group(1)
        dossier = dossier.encode().decode("unicode-escape")
        dossier = json.loads(dossier)

        if dossier['room_status'] == 'offline':
            logger.info('Room is offline!')
            return

        id0 = random.randint(0, 1000)
        id1 = ''.join(random.choice(string.ascii_lowercase) for x in range(8))

        self.wschat = dossier['wschat_host']
        self.wschat = self.wschat.replace('https://', 'wss://')
        self.wschat = '{}/{}/{}/websocket'.format(self.wschat, id0, id1)
        self.username = dossier['chat_username']
        self.password = dossier['chat_password']
        self.roomPassword = dossier['room_pass']

    async def run(self):
        logger.info('connecting to websocket: %s', self.wschat)
        async with websockets.connect(self.wschat) as ws:
            self.connectedTime = time.perf_counter()
            while True:
                message = await ws.recv()
                logger.debug('received: %s', message)

                if not message:
                    continue              

                await self.process(ws, message)

    async def process(self, ws, message):
        if message[0] == 'o':
            logger.info('sending connect')
            await ws.send(self.createConnectedMessage())
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

                await ws.send(self.createAuthResponseMessage(result))
            elif data['method'] == 'onNotify':
                args = json.loads(data['args'][0])
                if args['type'] == 'tip_alert':
                    ignored = time.perf_counter() - self.connectedTime < 2
                    logger.info('received tip_alert %s %d %s', args['from_username'], args['amount'], 'ignored' if ignored else '')
                    if not ignored:
                        loop.call_later(settings['delay'], self.pushTip, args['amount'])
            elif data['method'] == 'onRoomMsg':
                pass 

    def createMessage(self, method, data):
        message = json.dumps({
            'method': method,
            'data': data
        })
        message = json.dumps([message])
        logger.debug('sending: %s', message)
        return message

    def createConnectedMessage(self):
        return self.createMessage('connect', {
            'user': self.username,
            'password': self.password,
            'room': settings['room'],
            'room_password': self.roomPassword
        })

    def createAuthResponseMessage(self, result):
        return self.createMessage('joinRoom', {
            'room': settings['room']
        })

    def pushTip(self, amount):
        self.queue.put_nowait(amount)

loop = asyncio.new_event_loop() 
q = asyncio.Queue(loop=loop)  

chaturbate = Chaturbate(loop, q)
device = TipMenuDevice(loop, q)
try:
    loop.create_task(chaturbate.run())
    loop.create_task(device.run())

    loop.run_forever()
finally:
    loop.close()