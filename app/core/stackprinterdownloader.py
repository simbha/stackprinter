import re, logging

from google.appengine.api import memcache
from google.appengine.api import urlfetch
from google.appengine.ext import deferred

import web 
from app.config.constant import UNSUPPORTED_SERVICE_ERROR
import app.lib.sepy as sepy
import app.lib.deliciousapi as deliciousapi 
import app.utility.utils as utils
from app.utility.utils import memcached
import app.utility.worker as worker
import app.db.question as dbquestion

VOTES_ENTRY_LEVEL = 15

deferred.deferred._TASKQUEUE_HEADERS['X-AppEngine-FailFast'] = 'True'

class Post(object):
    def __init__(self, question, answers):
        self.question = question
        self.answers = answers
        self.deleted = False
    def is_printable(self):
        if self.question is None or self.answers is None:
            return False
        else:
            return True
    def is_deleted(self):
        return self.deleted
            
class Question(object):
    def __init__(self, question_id, url, title, tags_list, creation_date, service, up_vote_count = 0, down_vote_count = 0, answer_count = 0 ):
        self.question_id = question_id
        self.url = url
        self.title = title
        self.tags_list = tags_list
        self.creation_date = creation_date
        self.service = service
        self.up_vote_count = up_vote_count
        self.down_vote_count = down_vote_count
        self.answer_count = answer_count
    def get_votes(self):
        return '%s%d' % (['','+'][self.up_vote_count-self.down_vote_count > 0],self.up_vote_count-self.down_vote_count)

class UnsupportedServiceError(Exception):
    def __init__(self, service, message):
        self.args = (service, message)
        self.service = service
        self.message = message

class StackExchangeDownloader():
    def __init__(self, service):
        if service not in StackAuthDownloader.get_supported_services().keys:
            raise UnsupportedServiceError(service, UNSUPPORTED_SERVICE_ERROR)
        self.service = service
        self.api_site_parameter = StackAuthDownloader.get_supported_services().info[self.service]['api_site_parameter']
        self.retriever = sepy
        
    def get_question(self, question_id):  
        results = self.retriever.get_question(int(question_id), self.api_site_parameter, body = True, comments = True, pagesize = 1)
        question = results["items"]
        if len(question) > 0 and question[0].has_key('title'):           
            if int(question[0]['up_vote_count'])-int(question[0]['down_vote_count']) > VOTES_ENTRY_LEVEL :
                try:
                    deferred.defer(worker.deferred_store_question_to_cache, question_id, self.service, question[0])
                except:
                    logging.info("%s - defer error trying to store question_id : %s" % (self.service, question_id))
            else:
                try:
                    logging.info("%s trying to delete question and answers with question_id : %s" % (self.service, question_id)) 
                    deferred.defer(worker.deferred_delete_question_and_answers, question_id, self.service)
                except:
                    logging.info("%s - defer error trying to delete question and answers with question_id : %s" % (self.service, question_id))   

            return question[0]
        else:
            return None
                    
    def get_question_title(self, question_id):  
        results = self.retriever.get_question(int(question_id), self.api_site_parameter, pagesize = 1)
        question = results["items"]
        if len(question) > 0 and question[0].has_key('title') :
            return question[0]['title']
        else:
            return None
    
    def get_question_quicklook(self, question_id):
        results = self.retriever.get_question(int(question_id), self.api_site_parameter, body = True, comments = False, pagesize = 1)
        question = results["items"]
        if len(question) > 0 and question[0].has_key('title'):
            return question[0]
        else:
            return None
            
    def get_answer_quicklook(self, answer_id):
        results = self.retriever.get_answer(int(answer_id), self.api_site_parameter, body = True, comments = False, pagesize = 1)
        answer = results["items"]
        if len(answer) > 0:
            return answer[0]
        else:
            return None
    
    def get_questions_by_hotness(self, page = 1 , pagesize = 30, sort = 'week'):
        questions_by_hotness = []
        results = self.retriever.get_questions(self.api_site_parameter, page, pagesize, sort)
        questions = results["items"]
        return questions
            
    def get_questions_by_tags(self, tagged, page):
        questions_by_tags = []
        results = self.retriever.get_questions_by_tags(";".join([web.net.urlquote(tag) for tag in tagged.strip().split()]), self.api_site_parameter, page, pagesize = 30)
        questions = results["items"]
        pagination = utils.Pagination(results)
        for question in questions:
            questions_by_tags.append(Question(question['question_id'],
                                   "http://%s.com/questions/%d" % (self.service, question['question_id']),
                                   question['title'],
                                   question['tags'],
                                   utils.date_from(question['creation_date']),
                                   self.service,
                                   question['up_vote_count'],
                                   question['down_vote_count'],
                                   question['answer_count']
                                   ))
        return (questions_by_tags, pagination)

    def get_questions_by_votes(self, page):
        questions_by_votes = []
        results = self.retriever.get_questions(self.api_site_parameter, page, pagesize = 30)
        questions = results["items"]
        pagination = utils.Pagination(results)
        for question in questions:
            questions_by_votes.append(Question(question['question_id'],
                                   "http://%s.com/questions/%d" % (self.service, question['question_id']),
                                   question['title'],
                                   question['tags'],
                                   utils.date_from(question['creation_date']),
                                   self.service,
                                   question['up_vote_count'],
                                   question['down_vote_count'],
                                   question['answer_count']
                                   ))
        return (questions_by_votes, pagination)     
    """     
    Deprecated: synchronous answers download  
    def get_answers(self, question_id): 
        answers = []
        page = 1
        while True:
            results = self.retriever.get_answers(int(question_id), self.api_site_parameter, body = True, comments = True, pagesize = 50, page = page, sort = 'votes')
            answers_chunk = results["answers"] 
            answers = answers + answers_chunk
            if len(answers) == int(results["total"]):
                break
            else:
                page = page +1 
        return answers"""
    
    def get_answers(self, question_id, persist = True):  
        answers_chunk_dict = {}
        answers = []
        page = 1
        pagesize = 50
        
        total_answers = int(self.retriever.get_answers(int(question_id), self.api_site_parameter, body = False, comments = False, pagesize = 0)['total'])   
        if total_answers <= pagesize:
            results = self.retriever.get_answers(int(question_id), self.api_site_parameter, body = True, comments = True, pagesize = pagesize, page = page, sort = 'votes')
            answers = results["items"]
        else:
            def handle_result(rpc, page):
                result = rpc.get_result()
                response = sepy.handle_response(result,url = '/answers?page%s' % page) #TODO:find a way to pass the complete url
                answers_chunk = response["items"]
                answers_chunk_dict[page] = answers_chunk

            def create_callback(rpc, page):
                return lambda: handle_result(rpc, page)
            rpcs = []
            while True:
                rpc = urlfetch.create_rpc(deadline = 10)
                rpc.callback = create_callback(rpc, page)
                self.retriever.get_answers(int(question_id), self.api_site_parameter, rpc = rpc, body = True, comments = True, pagesize = pagesize, page = page, sort = 'votes')
                rpcs.append(rpc)
                if pagesize * page > total_answers:
                    break
                else:
                    page = page +1   
            for rpc in rpcs:
                rpc.wait()
            page_keys = answers_chunk_dict.keys()
            page_keys.sort()
            for key in page_keys:
                answers= answers + answers_chunk_dict[key]
        
        try:
            #cache it to db (does not work for payload bigger than 1MByte)
            if persist:
                deferred.defer(worker.deferred_store_answers_to_cache, question_id, self.service, answers)
        except:
            logging.info("%s - defer error trying to store answers of question_id : %s" % (self.service, question_id))
            
        return answers
        
    def get_users_by_id(self, user_id):   
        results = self.retriever.get_users_by_id(int(user_id), self.api_site_parameter, page = 1, pagesize = 1)
        users = results['items']
        return users
        
    def get_users(self, username_filter):    
        results = self.retriever.get_users(web.net.urlquote(username_filter), self.api_site_parameter, pagesize = 50)
        users = results['items']
        return users
    
    def get_favorites_questions(self, user_id, page): 
        favorites_questions = []
        results = self.retriever.get_favorites_questions(user_id, self.api_site_parameter, page, pagesize= 30)
        questions = results["items"]
        pagination = utils.Pagination(results)
        for question in questions:
            favorites_questions.append(Question(question['question_id'],
                                   "http://%s.com/questions/%d" % (self.service, question['question_id']),
                                   question['title'],
                                   question['tags'], 
                                   utils.date_from(question['creation_date']),
                                   self.service,
                                   question['up_vote_count'],
                                   question['down_vote_count'],
                                   question['answer_count']
                                   ))
        return (favorites_questions, pagination)
    
    def get_asked_questions(self, user_id, page): 
        asked_questions = []
        results = self.retriever.get_asked_questions(user_id, self.api_site_parameter, page, pagesize= 30)
        questions = results["items"]
        pagination = utils.Pagination(results)
        for question in questions:
            asked_questions.append(Question(question['question_id'],
                                   "http://%s.com/questions/%d" % (self.service, question['question_id']),
                                   question['title'],
                                   question['tags'], 
                                   utils.date_from(question['creation_date']),
                                   self.service,
                                   question['up_vote_count'],
                                   question['down_vote_count'],
                                   question['answer_count']
                                   ))
        return (asked_questions, pagination)
    
    def get_answered_questions(self, user_id, page): 
        asked_questions = []
        results = self.retriever.get_user_anwers(user_id, self.api_site_parameter, page, pagesize= 30, sort = 'creation')
        answers = results["items"]
        if answers:
            pagination = utils.Pagination(results)
            question_ids = ';'.join([str(answer['question_id']) for answer in answers])
            results = self.retriever.get_questions_by_ids(question_ids, self.api_site_parameter, page, pagesize= 30)
            questions = results["items"]
        else:
            questions = []
            pagination = None
        for question in questions:
            asked_questions.append(Question(question['question_id'],
                                   "http://%s.com/questions/%d" % (self.service, question['question_id']),
                                   question['title'],
                                   question['tags'], 
                                   utils.date_from(question['creation_date']),
                                   self.service,
                                   question['up_vote_count'],
                                   question['down_vote_count'],
                                   question['answer_count']
                                   ))
        return (asked_questions, pagination)
    
        
    def get_tags(self, tag_filter):
        results = self.retriever.get_tags(tag_filter, self.api_site_parameter, page = 1, pagesize = 10)
        tags = results['items']
        return "\n".join([tag['name'] for tag in tags ])
    
    @memcached('get_post', 3600*24*20, lambda self, question_id, bypass_cache : "%s_%s" % (self.service,question_id), None, lambda self, question_id, bypass_cache : bypass_cache)
    def get_post(self, question_id, bypass_cache = False):
       """
          Return a post object representing the question and the answers list 
          Return None if question/answers are not found from Api call or db cache (deleted questions)
       """
       try:
           question = self.get_question(question_id)
           if question:
               post = Post(question, self.get_answers(question_id, int(question['up_vote_count'])-int(question['down_vote_count']) > VOTES_ENTRY_LEVEL))    
           else: #StackPrinter loves the legendary deleted questions 
               post = Post(dbquestion.get_question(question_id, self.service),
                           dbquestion.get_answers(question_id, self.service))
               post.deleted = True
       except (sepy.ApiRequestError, urlfetch.DownloadError): 
            post = Post(dbquestion.get_question(question_id, self.service),
                        dbquestion.get_answers(question_id, self.service)) 
            if not post.is_printable():
                raise
            
       if post.is_printable():
           try:
               deferred.defer(worker.deferred_store_print_statistics,
                                 post.question['question_id'], 
                                 self.service, 
                                 post.question['title'], 
                                 post.question['tags'],
                                 post.deleted)
           except:
               logging.info("%s - defer error trying to store print statistics : %s" % (self.service, question_id))
           
           return post
       else:
           return None
           
           
class StackAuthDownloader():    
    @staticmethod    
    def get_supported_services():
        supported_services = memcache.get("supported_services")
        if supported_services is not None:
            return supported_services
        else:
            results = sepy.get_sites()
            supported_services = utils.get_supported_services(results['items'])
            memcache.set("supported_services", supported_services, 14400) #Recheck at least every four hours
            return supported_services
    
    @staticmethod    
    def renew_auth_token():
        """ Get a new AuthToken storing it"""
        token_parameter = sepy.get_auth_token()
        token = token_parameter.split('=')[1]
        return utils.TokenManager.store_auth_token(token)
    @staticmethod    
    def invalidate_auth_token(auth_token):
        """ Invalidate auth token"""
        return sepy.invalidate_auth_token(auth_token)
        
class DeliciousDownloader():  
    def get_favorites_questions(self, username):
        result = [] 
        dapi = deliciousapi.DeliciousAPI()
        meta = dapi.get_user(username, max_bookmarks = 100)
        bookmarks = meta.bookmarks
        for bookmark in bookmarks:
            match = re.search('http://(%s)\.com/questions/(\d+)/' % ("|".join(StackAuthDownloader.get_supported_services().keys).replace(".","\.")), bookmark[0])
            if match:
                result.append(Question(match.group(2), bookmark[0], bookmark[2], bookmark[1], bookmark[4], match.group(1)))
        return result