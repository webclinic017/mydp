
import asyncio
import time

import nest_asyncio
nest_asyncio.apply()

import pandas as pd

from src.client.mexc.spot_api_client import MexcSpotApiClient
from src.client.mexc.spot_socket_client import MexcSpotSocketClient
from src.base.types import DataEventFuncType
from src.base.interfaces import ExchangeProxy
from src.base.results import ServiceResult
import src.base.errors as error

class MexcSpotProxy(ExchangeProxy):

    def __init__(self, exchange_name: str, symbols_config: 'list[dict]', push_data_event_func: DataEventFuncType):
         
        self.__exchange_name = exchange_name

        self.__data: 'dict[tuple[str, str], pd.DataFrame]' = {}

        self.__symbols_config: 'dict[str, tuple(list, list)]' = \
            { conf['symbol']: (conf['timeframes'], conf['aliases']) for conf in symbols_config }                      

        self.__push_data_event_func = push_data_event_func

        self.__api_client: MexcSpotApiClient = MexcSpotApiClient()
        self.__socket_client: MexcSpotSocketClient = MexcSpotSocketClient()                      
               
        loop = asyncio.get_event_loop()        
        loop.create_task(self.__prepare_historical_data())                 


#%% Historical data setup.


    async def __prepare_historical_data(self):
        
        symbols = self.__symbols_config.keys()
        for symbol in symbols:            
            timeframes = self.__symbols_config[symbol][0]
            for timeframe in timeframes:
                df = await self.__fetch_kline(symbol, timeframe)
                self.__data[(symbol, timeframe)] = df # df[::-1]
        
        await self.__connect_to_data_streams()                

        
    async def __fetch_kline(self, symbol, timeframe):
        
        klines = await self.__api_client.get_klines(symbol=symbol, timeframe=timeframe)      
        df = pd.DataFrame(klines)
        df.drop(df.columns[[6, 7]], axis=1, inplace=True)  # Remove unnecessary columns
        df = self.__parse_dataframe(df)

        return df


    def __parse_dataframe(self, df_klines):   
        
        df = df_klines.copy()
        df.columns = ['open_timestamp', 'open', 'high', 'low', 'close', 'volume']                    

        df['open_datetime'] = pd.to_datetime(df['open_timestamp'], unit='ms')
        df = df.set_index('open_datetime')  

        df['open'] = df['open'].astype('float')
        df['high'] = df['high'].astype('float')
        df['low'] = df['low'].astype('float')
        df['close'] = df['close'].astype('float')
        df['volume'] = df['volume'].astype('float')        

        df = df[['open_timestamp', 'open', 'high', 'low', 'close', 'volume']]     
        
        return df


#%% Socket setup.


    async def __connect_to_data_streams(self):        
        
        await self.__initialize_socket_client()     

        streams = []
        
        symbols = self.__symbols_config.keys()     
        for symbol in symbols:

            tupple_tfs_aliases = self.__symbols_config[symbol]

            for timeframe in tupple_tfs_aliases[0]:

                stream = {
                    "symbol": symbol,                  
                    "interval": self.__get_socket_interval_from_timeframe(timeframe),
                    "callback": self.__handle_socket_message
                }
                           
                streams.append(stream)
    
        await self.__subscribe_to_topics(streams)       


    def __handle_socket_message(self, msg):    
        
        """https://mxcdevelop.github.io/apidocs/spot_v3_en/#kline-streams"""
        
        if ('c' in msg) and ('spot@public.kline.v3.api' in msg['c']):                
            self.__handle_data_event(msg)

        else: 
            if ('msg' in msg) and (msg['msg'] == 'PONG'):
                print('pong received.')

            else:              
                #TODO: log error
                print(msg)

    def __handle_data_event(self, msg): 
        
        channel = msg['c']  
        market_channel_symbol_interval = channel.split('@')
        symbol = market_channel_symbol_interval[2]      
        interval = market_channel_symbol_interval[3]
        timeframe = self.__get_timeframe_from_socket_interval(interval)
        kline = msg['d']['k'] 
        
        candle = {
            'open_timestamp': kline['t'],
            'open_datetime': pd.to_datetime(kline['t'], unit='s'),
            'open': kline['o'],
            'high': kline['h'],
            'low': kline['l'],
            'close': kline['c'],
            'volume': kline['v']
        }

        row = pd.DataFrame.from_records(data=[candle], index='open_datetime')

        if (symbol, timeframe) in self.__data:
            df = self.__data[(symbol, timeframe)]            
            df_new = row.combine_first(df).tail(500)
            self.__data[(symbol, timeframe)] = df_new
        else:
            self.__data[(symbol, timeframe)] = row

        candle['open_datetime'] = str(candle['open_datetime'])      

        loop = asyncio.get_event_loop()
        loop.create_task(self.__push_data_event_func(self.__exchange_name, symbol, timeframe, candle))


    async def __initialize_socket_client(self):

        await self.__socket_client.init()  

        loop = asyncio.get_event_loop()        
        loop.create_task(self.__socket_client.ping())   
          
    async def __subscribe_to_topics(self, list_topics: 'list[dict]'):
        
        loop = asyncio.get_event_loop()        
         
        for topic in list_topics:  
            loop.create_task(self.__socket_client.kline_subscribe(**topic))          
            # await self.__socket_client.kline_subscribe(**topic)             


    def __get_socket_interval_from_timeframe(self, timeframe):

        timeframe_to_interval = {
            '1m': 'Min1',
            '5m': 'Min5',
            '15m': 'Min15',
            '30m': 'Min30',
            '60m': 'Min60',
            '4h': 'Hour4',
            '1d': 'Day1',
            '1M': 'Month1'
        }

        return timeframe_to_interval[timeframe]
    
    def __get_timeframe_from_socket_interval(self, interval):

        interval_to_timeframe = {
            'Min1': '1m',
            'Min5': '5m',
            'Min15': '15m',
            'Min30': '30m',
            'Min60': '60m',
            'Hour4': '4h',
            'Day1': '1d',
            'Month1': '1M'
        }

        return interval_to_timeframe[interval]
            

#%% Data methods.


    def __get_symbol_config(self, symbol_name):        

        if symbol_name in self.__symbols_config:
            return symbol_name, self.__symbols_config[symbol_name]
        
        else:
            for key in self.__symbols_config:
                symbol_config = self.__symbols_config[key]               
                if symbol_name in symbol_config[1]:
                    return key, symbol_config
        
        return None, None


    def get_candles(self, symbol_name: str, timeframe: str, count: int) -> ServiceResult[pd.DataFrame]:

        result = ServiceResult[pd.DataFrame]()

        config_key, symbol_config = self.__get_symbol_config(symbol_name)     

        if symbol_config is None:
            result.success = False
            result.message = error.INVALID_SYMBOL
            return result

        if timeframe not in symbol_config[0]:
            result.success = False
            result.message = error.INVALID_TIMEFRAME
            return result

        key = (config_key, timeframe)
        df = self.__data[key].tail(count).copy()
        df = df.reset_index()
        df = df[['open_timestamp', 'open_datetime', 'open', 'high', 'low', 'close', 'volume']]  

        result.success = True
        result.result = df

        return result
        
        
