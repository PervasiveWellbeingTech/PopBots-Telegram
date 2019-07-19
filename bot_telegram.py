#/usr/bin/env python3
# -*- coding: UTF-8 -*-
"""
This file communicates between the database and the API (Telegram).

Created by Nick Tantivasadakarn.
"""

import time
import random
import string
#import sys, getopt
#import os, nltk
import re

from collections import defaultdict
#from os import system
from utils import Params, Config, Modes, find_keyword, find_name, find_id, find_problem
from get_response import get_response_dict
from pymongo import MongoClient
import telegram
from telegram.error import NetworkError, Unauthorized
from time import sleep



TIMEOUT_SECONDS = 3600

class TelegramBot():
    """
    Implementation of the bot.

    Initialization parameter:
        token (string) -- Telegram bot token

    Class parameters:
        self.bot (telegram.bot) -- telegram bot object
        self.reply_dict (dict) -- dictionary containing all responses
        self.params (utils.Params) -- fixed parameters
        self.config (utils.Config) -- bot configuration

        self.user_history(defaultdict) -- temporary dictionary of user to user history
        self.user_name_dict(defaultdict) -- user_id to name dictionary
        self.user_bot_state_dict (defaultdict) -- user_id to bot state dict (defaults to a random bot)
        self.user_problem_dict(dict) -- user_id to problem dictionary
        self.user_parameters_dict(defaultdict) -- dictionary that stores all parameters used by the bots.
    """

    def __init__(self, token): #, reply_dict, **kwargs):
        print("Bot initialization.")
        #initialize telegram bot
        self.bot = telegram.Bot(token)
        try:
            self.update_id = self.bot.get_updates()[0].update_id
        except IndexError:
            self.update_id = None

        #initialize bot responses and parameters
        self.reply_dict = get_response_dict()
        self.params = Params()
        self.config = Config()

        #initialize database
        if self.params.MODE == Modes.TEXT:
            self.db = MongoClient().textbot_telegram
        else:
            self.db = MongoClient().voicebot_telegram

        #initialize user info 
        self.user_history = defaultdict(list)
        #self.user_name_dict = self.load_names(self.db.user_history)
        self.user_bot_state_dict = defaultdict(lambda:(None, None))
        self.user_problem_dict = {}


        self.allow_choice = True 
        #self.user_parameters_dict = self.load_user_parameters(self.db.user_history)
        self.user_name_dict, self.user_parameters_dict, self.ids = self.load_parameters(self.db.user_history)

        keyboards =[telegram.InlineKeyboardButton("Choose for me")]+[
                                telegram.InlineKeyboardButton(name) for idx, name in enumerate(self.params.bot_name_list) if idx not in {4,7}]
        self.bots_keyboard = [ [x,y] for x,y in zip(keyboards[0::2], keyboards[1::2]) ]
        if len(keyboards)%2 ==1:
            self.bots_keyboard.append([keyboards[-1]])


    def load_parameters(self, collection):
        """
        Loads names and user parameter from database

        Parameter:
            collection (mongo collection)

        Returns:
            (dict) user_id to user_name dictionary
        """
        names = defaultdict(lambda: '')
        parameters = defaultdict(dict)
        ids = defaultdict(lambda: None)
        for hist in collection.find():
            names[hist['user_id']] = hist.get('user_name', '')
            parameters[hist['user_id']] = hist.get('user_parameters', {})
            ids[hist['user_id']] = hist.get('subject_id', None)
        return names, parameters, ids


    # def load_user_parameters(self, collection):
    #     """
    #     Loads user parameters from database

    #     Parameter:
    #         collection (mongo collection)

    #     Returns:
    #         (dict) user_id to user_parameter dictionary
    #     """
    #     parameters = {}
    #     for hist in collection.find():
    #         parameters[hist['user_id']] = hist['user_parameters']
    #     return parameters

    def process_updates(self, bot_updates):
        """
        Handles interactions with the Telegram server.
        Receives all the user replies in the form of updates and tells
        Telegram what to reply

        Parameters:
             bot_updates (iterator) -- all updates from bot.get_updates function
        """
        # Request updates after the last update_id
        for update in bot_updates:
            self.update_id = update.update_id + 1
            if update.message and update.message.text: #ignores all non-text inputs
                user_id = update.message.chat_id
                query = update.message.text 
                self.process_message(user_id, query)  
                    
    def process_message(self, user_id, query):
        ############ Special Cases #######################
        if re.match(r'/start', query): #restart
            self.log_action(user_id, None, None, "RESET", "")
            self.save_history_to_database(user_id)
            self.user_history.pop(user_id, None)
            self.user_bot_state_dict[user_id] = (7 , self.config.START_INDEX)
            subj_id = re.findall(' ([0-9]+)', query)
            if subj_id:
                self.set_subj_id(user_id, int(subj_id[0]))
            self.set_choice(user_id)
            self.user_problem_dict.pop(user_id, None)

        elif self.conversation_timeout(user_id): #Time out
            self.log_action(user_id, None, None, "<TIMEOUT>", "")
            self.save_history_to_database(user_id)
            self.user_parameters_dict[user_id]['last']=self.user_bot_state_dict[user_id][0]
            self.user_history.pop(user_id, None)
            self.user_bot_state_dict.pop(user_id, None)
            self.user_problem_dict.pop(user_id, None)

        elif re.match(r'/switch', query): #switch
            self.log_action(user_id, None, None, "<SWITCH>", "")
            self.save_history_to_database(user_id)
            self.user_parameters_dict[user_id]['last']=self.user_bot_state_dict[user_id][0]
            self.user_history.pop(user_id, None)
            self.user_bot_state_dict[user_id] = (7,3)

        
        ############ Normal Cases #######################
        bot_id, response_id = self.get_next(user_id, query)

        if response_id == self.config.CLOSING_INDEX:
            self.log_action(user_id, bot_id, response_id, "<CONVERSATION_END>", query)
            self.save_history_to_database(user_id)
        
        if response_id == None: #End of conversation"
            self.user_history.pop(user_id, None)
            self.user_parameters_dict[user_id]['last']=bot_id
            if find_keyword(query, self.config.GREETINGS): #the user activates another bot
                self.user_bot_state_dict[user_id] = (None, None)
                bot_id, response_id = self.get_next(user_id, query)
            else:
                self.user_bot_state_dict.pop(user_id, None)
        #extract names
        if bot_id == 7 and response_id == 2:
            name = find_name(query)
            self.user_name_dict[user_id] = name
            self.db.user_history.update_one({'user_id':user_id}, {'$set':{'user_name': name}},
                             upsert=True)
        
        #Set custom keyboard (defaults to none)
        reply_markup = telegram.ReplyKeyboardRemove()
        
        #get problem
        if  (bot_id == 7 and response_id == 4 and not self.user_parameters_dict[user_id].get('choice_enabled', False)) or (bot_id == 7 and response_id == 3 and  self.user_parameters_dict[user_id].get('choice_enabled', False)):
            problem = find_problem(query)
            if problem:
                self.user_problem_dict[user_id] = problem

        #show choices
        if  bot_id == 7 and response_id == 3 and self.user_parameters_dict[user_id].get('choice_enabled', False):
            bots_keyboard = self.bots_keyboard
            reply_markup = telegram.ReplyKeyboardMarkup(bots_keyboard, resize_keyboard= True)               

        if response_id in {self.config.CLOSING_INDEX, self.config.ABRUPT_CLOSING_INDEX}:
            reply_markup = telegram.ReplyKeyboardMarkup([[telegram.InlineKeyboardButton("Hi")]], resize_keyboard= True)
        
        #select bot
        if bot_id == 7 and response_id == 4:
            self.post_and_log_text(bot_id, response_id, user_id, query, reply_markup)
            bot_choice = self.params.bot2id.get(query, None)
            #reply_markup = telegram.ReplyKeyboardRemove()
            if type(bot_choice) == int:
                bot_id  = bot_choice
            else:
                last = self.user_parameters_dict[user_id].get('last', None)
                bot_id = self.recommend_bot(last)
            response_id = self.config.OPENNING_INDEX


        #handle images
        if self.params.MODE == Modes.TEXT and response_id == self.config.OPENNING_INDEX:
            img = open('img/{}.png'.format(bot_id), 'rb')
            self.bot.send_photo(chat_id=user_id, photo=img)

        if bot_id == 7 and response_id == 6:
            img = open('img/{}.png'.format(bot_id), 'rb')
            self.bot.send_photo(chat_id=user_id, photo=img)
        

        #handle text responses
        self.post_and_log_text(bot_id, response_id, user_id, query, reply_markup)
        #To skip 
        if bot_id == 7 and response_id == 4:
            self.process_message(user_id, "<SKIP>")


    def set_subj_id(self, user_id:int, subject_id:int):
        """
        Parameters:
            user_id(int) -- unique user identifye
            subject_id(int) -- subject id (MUST be a non-indentifiable number)
        """
        self.db.user_history.update_one({'user_id':user_id}, {'$set':{'subject_id': subject_id}},
                     upsert=True)

        self.ids[user_id] = subject_id

    def set_choice(self, user_id:int):
        """
        Set subjects to either the choice (able to choose bots) or non-choice group.
        Parameters:
            user_id(int) -- unique user identifyer
        """
        if 'choice_enabled' not in self.user_parameters_dict[user_id]:
            if self.allow_choice:
                self.db.user_history.update_one({'user_id':user_id}, {
                            '$set':{'user_parameters': {'choice_enabled': True}}},
                            upsert=True)
                self.user_parameters_dict[user_id]['choice_enabled'] = True
                self.allow_choice = False
            else:
                self.db.user_history.update_one({'user_id':user_id}, {
                            '$set':{'user_parameters': {'choice_enabled': False}}},
                            upsert=True)
                self.user_parameters_dict[user_id]['choice_enabled'] = False
                self.allow_choice = True


    def post_and_log_text(self, bot_id, response_id, user_id, query, reply_markup = None):
        """
        Posts the appropriate text to Telegram, and logs the conversation

        Parameters:
            bot_id(int) -- bot id
            response_id(int) -- response within bot
            user_id(int) -- unique identifyer
            query(string) -- user input
        """
        if response_id != None:
            text_response = self.get_text_response(bot_id, response_id)
            text_response_format = list(self.replace_entities(text_response, user_id, bot_id))
            for res in text_response_format:
                self.bot.send_message(chat_id=user_id, text=res, reply_markup = reply_markup)
            self.log_action(user_id, bot_id, response_id, text_response_format, query)
            self.user_bot_state_dict[user_id] = (bot_id, response_id)


    def get_text_response(self, bot_id, response_id):
        """
        Processes the input text and returns the response.

        This function looks at the state  within the conversation,
        and outputs the appropriate response. 

        Parameters:
            bot_id(int) -- id of the bot
            response_id(int) -- id of the response within a bot

        Returns:
            (list) -- list of strings the responses 
        """
        response_dict =  self.reply_dict[bot_id][response_id].texts
        #get text of the selected mode
        response_choices = response_dict.get(self.params.MODE, self.reply_dict[bot_id][response_id].texts[Modes.GENERAL])
        response = random.choice(response_choices)

        return response

    def replace_entities(self, responses, user_id, bot_id):
        """
        Replaces entity place holders such as {name} {bot_name} 
        with appropriate names

        Parameters:
            responses(list) -- list of text responses
            user_id(int) -- user unique identifyer
            bot_id(int) -- id of the bot 

        Returns:
            (list) -- list of strings the responses 
        """
        name = self.user_name_dict.get(user_id, '')
        problem = self.user_problem_dict.get(user_id, 'that')
        bot_name = self.params.bot_name_list[bot_id]
        subject_id = self.ids[user_id]
        for res in responses:
            yield res.format(name=name, problem=problem, bot_name=bot_name, subject_id=subject_id)


    def get_next(self, user_id, query):
        """
        Determines the next state in the conversation.

        NOTE: If next_id is a list of branching paths, 
        it will select the first path that has a matching keyword in the query.
        (DEFAULT_OTHERS will always match, so put it at the end)

        Parameters:
            user_id (int) -- user unique identifyer
            query (string) -- user input string.
        """
        #get current id
        (bot_id, response_id) = self.user_bot_state_dict[user_id]
        if bot_id == None and response_id == None:
            return 7, 6

        if bot_id == 7 and response_id == 6:
            if self.user_parameters_dict[user_id].get('choice_enabled', False): #go to choice selection
                return 7, 3
            else:
                return 7, 4
        next = self.reply_dict[bot_id][response_id].next_id
        if not next:
            next_id = None
        elif type(next) == list: #handle branching paths
            next_id = None
            for keywords, value in next:
                if keywords == self.config.DEFAULT_OTHERS:
                    next_id = value
                    break
                elif find_keyword(query, keywords):
                    #prevent catching long answers containing the word 'no'
                    if keywords != self.config.DEFAULT_NO or (len(query.split(" ")) <= 5 and len(query) <= 25):
                        next_id = value
                        break
            assert next_id != None, "No dialog option match."
        elif type(next) == int: #handle single path
            next_id = next
        else: #if no patters match
            raise ValueError
        return bot_id, next_id

    def log_action(self, user_id, bot_id, response_id, replies, query):
        """
        Creates a log of the interaction and saves it to self.user_history

        Parameters:
            user_id(int) -- unique identifyer
            bot_id(int) -- id of the bot
            response_id(int) -- id of the response within a bot
            replies(list/iterator) -- list/iterator of text that the bot replies to the user
            query(string) -- user input

            Note: it also logs a time stamp 
        """
        new_entry = {
                        'bot_id':bot_id, 
                        'response_id':response_id,
                        'query':query,
                        'replies':list(replies),
                        'time':time.time()
                    }
        self.user_history[user_id].append(new_entry)

    def recommend_bot(self, last=None):
        """
        Recommends a random bot except bot id 7 (onboarding bot) 
        and bot id 4 (relaxation bot) and the last used bot.

        Return:
            bot id (int)

        Notes: Refer to get_resposne.py or utils.py (bot_name_list) to get the list.
        """
        return random.choice([i for i in range(self.params.BOT_NUM) if i not in {4,7,last}])

    def conversation_timeout(self, user_id):
        """
        Check if the conversation has timed out.

        Parameter:
            user_id(int) -- unique identifyer

        Return:
            (bool) -- True if the conversation has timed out
        """
        if self.user_history[user_id]:
            last_entry_time = self.user_history[user_id][0]['time']
            return time.time()-last_entry_time >= TIMEOUT_SECONDS
        else:
            return False

    def save_history_to_database(self, user_id):

        """
        This function sends the data from the user history (pulled from the class variable)
        to the database

        Parameter:
            user_id(int) -- unique identifyer
        """
        history = self.user_history[user_id]
        self.db.user_history.update_one({'user_id':user_id},
                                        {"$push":{'user_history': history}}
                                    )

    def run(self):
        """
        Run the bot.
        """
        print("Running Bot ... (Ctrl-C to exit)")
        while True:
            #Check if there are updates.
            try:
                bot_updates = self.bot.get_updates(offset=self.update_id, timeout=60)
            except NetworkError:
                sleep(1)
                continue
            except Unauthorized:
                # The user has removed or blocked the bot.
                self.update_id += 1           
                continue
            #If succesful
            self.process_updates(bot_updates)

if __name__ == '__main__':
    # Telegram Bot Authorization Token
    f = open('token.txt')
    token = f.read()
    bot = TelegramBot(token)
    bot.run()

